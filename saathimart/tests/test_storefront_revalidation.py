"""
Storefront-revalidation smoke tests — prove the hub → Next.js webhook
speaks the exact contract the storefront's /api/revalidate route accepts:

  POST {base}/api/revalidate
  headers: x-revalidate-secret: <secret>     (required header, exact value)
  body:    {"tags": [...]}                    (array; allowlisted prefixes)

A real HTTP server (http.server on 127.0.0.1, random port) plays the
storefront: it records method/path/headers/body and replies per-test. The
wire tests call `storefront_cache._deliver_revalidation` synchronously —
in production the SAME function is what the `short`-queue job runs, so the
bytes on the wire are identical; only the scheduling differs (this dev
suite has no RQ workers).

The `notify_nextjs` wiring tests patch `_reval_settings` and
`saathimart.api.utils.safe_enqueue` instead of writing SaathiMart Settings
— zero Settings writes, nothing to restore, no cross-test leakage.

Run:
    bench --site <site> run-tests --app saathimart
"""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

import frappe

from saathimart.api import storefront_cache


# ── Mock storefront: a real HTTP receiver that records requests ─────────────

class _Receiver(BaseHTTPRequestHandler):
    """Records every POST; `response_code` is a per-test knob."""

    captured = []       # list of {path, secret, body}
    response_code = 200

    def do_POST(self):  # noqa: N802 — http.server API
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        _Receiver.captured.append({
            "path": self.path,
            "secret": self.headers.get("x-revalidate-secret"),
            "body": body,
        })
        self.send_response(_Receiver.response_code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"revalidated": 1}')

    def log_message(self, fmt, *args):  # silence request logging
        pass


def _start_receiver():
    server = HTTPServer(("127.0.0.1", 0), _Receiver)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{port}"


class TestRevalidationWireContract(unittest.TestCase):
    """Real HTTP POSTs from the delivery function — the exact bytes the
    storefront route will see in production."""

    @classmethod
    def setUpClass(cls):
        cls.server, cls.base = _start_receiver()
        cls.url = f"{cls.base}/api/revalidate"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        _Receiver.captured = []
        _Receiver.response_code = 200

    def test_delivers_exact_contract(self):
        ok = storefront_cache._deliver_revalidation(
            url=self.url,
            secret="correct-secret",
            tags=["catalog-list", "catalog-product-tea-500g"],
            remark="smoke",
        )
        self.assertTrue(ok)
        self.assertEqual(len(_Receiver.captured), 1)
        req = _Receiver.captured[0]
        # exact endpoint path
        self.assertEqual(req["path"], "/api/revalidate")
        # exact header name + value (the route 401s without it)
        self.assertEqual(req["secret"], "correct-secret")
        # body is {"tags": [...]} — array, not the legacy {tag: ...} shape
        self.assertEqual(
            json.loads(req["body"]),
            {"tags": ["catalog-list", "catalog-product-tea-500g"]},
        )

    def test_sender_treats_401_as_failure(self):
        # The real route rejects a wrong/missing secret with 401.
        _Receiver.response_code = 401
        ok = storefront_cache._deliver_revalidation(
            url=self.url, secret="wrong-secret", tags=["catalog-list"],
            remark="smoke",
        )
        self.assertFalse(ok)
        self.assertEqual(_Receiver.captured[0]["secret"], "wrong-secret")

    def test_sender_treats_400_disallowed_tag_as_failure(self):
        # The real route 400s on unknown/disallowed tag prefixes.
        _Receiver.response_code = 400
        ok = storefront_cache._deliver_revalidation(
            url=self.url, secret="s", tags=["evil-tag"], remark="smoke",
        )
        self.assertFalse(ok)

    def test_delivery_survives_connection_errors(self):
        # Port 1 on loopback: connection refused, fast.
        ok = storefront_cache._deliver_revalidation(
            url="http://127.0.0.1:1/api/revalidate",
            secret="s", tags=["catalog-list"], remark="smoke",
        )
        # Logged, not raised, and honestly reported as not-delivered — a dead
        # storefront must never break a save, but must not claim success.
        self.assertFalse(ok)
        self.assertEqual(_Receiver.captured, [])


class TestNotifyWiring(unittest.TestCase):
    """The enqueue path: settings gating, dedupe, job arguments."""

    def setUp(self):
        _Receiver.captured = []

    def test_noop_when_disabled(self):
        with patch.object(
            storefront_cache, "_reval_settings",
            return_value={"enabled": False, "base_url": "", "secret": ""},
        ):
            sent = storefront_cache.notify_nextjs(["catalog-list"])
        self.assertFalse(sent)

    def test_noop_for_empty_tags(self):
        sent = storefront_cache.notify_nextjs([])
        self.assertFalse(sent)

    def test_enqueues_job_with_contract_args(self):
        with patch.object(
            storefront_cache, "_reval_settings",
            return_value={
                "enabled": True,
                "base_url": "http://fe.test",
                "secret": "s3cr3t",
            },
        ) as settings, patch(
            "saathimart.api.utils.safe_enqueue"
        ) as enqueue:
            sent = storefront_cache.notify_nextjs(
                ["catalog-list", "catalog-list", "cms-faq"],
                remark="unit",
            )

        self.assertTrue(sent)
        settings.assert_called()  # gating read happened
        args, kwargs = enqueue.call_args
        # the queued job is the same delivery function the wire tests prove
        self.assertEqual(args[0], storefront_cache._deliver_revalidation)
        self.assertEqual(kwargs["url"], "http://fe.test/api/revalidate")
        self.assertEqual(kwargs["secret"], "s3cr3t")
        # deduped tag list, order preserved
        self.assertEqual(kwargs["tags"], ["catalog-list", "cms-faq"])
        self.assertEqual(kwargs["remark"], "unit")

    def test_dedupe_preserves_order(self):
        self.assertEqual(
            storefront_cache._dedupe(
                ["b", "a", "b", "c", "a", ""],
            ),
            ["b", "a", "c"],
        )


class TestTagMapping(unittest.TestCase):
    """Hub doctypes → the Next.js tag universe (lib/cache-tags.ts)."""

    def test_product_tags(self):
        from types import SimpleNamespace
        self.assertEqual(storefront_cache.product_tags(None), ["catalog-list"])
        # No Product row resolvable → the name doubles as the slug fallback.
        with patch.object(
            storefront_cache.frappe, "db",
            SimpleNamespace(get_value=lambda *a, **k: None),
            create=True,
        ):
            self.assertEqual(
                storefront_cache.product_tags("PROD-001"),
                ["catalog-list", "catalog-product-PROD-001"],
            )

    def test_cms_tags(self):
        self.assertEqual(storefront_cache.cms_tags(), ["content-site"])
        self.assertEqual(storefront_cache.cms_tags(kind="faq"), ["cms-faq"])
        self.assertEqual(
            storefront_cache.cms_tags(slug="monsoon", kind="offers"),
            ["cms-offers", "cms-offer-monsoon"],
        )
        self.assertEqual(
            storefront_cache.cms_tags(slug="about", kind="page"),
            ["content-page:about", "content-site"],
        )
