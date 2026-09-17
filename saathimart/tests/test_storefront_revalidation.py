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
    """Records every POST; `response_codes` is a per-test sequence — one
    entry consumed per request, the last one repeating (so [503, 200]
    simulates a storefront that recovers between attempts)."""

    captured = []          # list of {path, secret, body}
    response_codes = [200]

    def do_POST(self):  # noqa: N802 — http.server API
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        _Receiver.captured.append({
            "path": self.path,
            "secret": self.headers.get("x-revalidate-secret"),
            "body": body,
        })
        codes = _Receiver.response_codes
        code = codes.pop(0) if len(codes) > 1 else codes[0]
        self.send_response(code)
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
        _Receiver.response_codes = [200]

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
        _Receiver.response_codes = [401]
        ok = storefront_cache._deliver_revalidation(
            url=self.url, secret="wrong-secret", tags=["catalog-list"],
            remark="smoke",
        )
        self.assertFalse(ok)
        self.assertEqual(_Receiver.captured[0]["secret"], "wrong-secret")

    def test_sender_treats_400_disallowed_tag_as_failure(self):
        # The real route 400s on unknown/disallowed tag prefixes.
        _Receiver.response_codes = [400]
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

    # ── Retry layer: transient retried, permanent not ─────────────────────

    def test_retries_transient_then_succeeds(self):
        # 429 then 503 then 200: both transient codes retried, delivery
        # succeeds on attempt 3. Tiny backoff keeps the test fast.
        _Receiver.response_codes = [429, 503, 200]
        ok = storefront_cache._deliver_revalidation(
            url=self.url, secret="s", tags=["catalog-list"],
            remark="smoke", attempts=4, base_delay=0.01,
        )
        self.assertTrue(ok)
        self.assertEqual(len(_Receiver.captured), 3)
        # every attempt carried the same contract
        self.assertTrue(all(r["path"] == "/api/revalidate" for r in _Receiver.captured))

    def test_does_not_retry_permanent_rejections(self):
        # 401/400/404 are contract mismatches — retrying cannot fix them,
        # so exactly ONE request must be made.
        for code in (401, 400, 404):
            with self.subTest(code=code):
                _Receiver.captured = []
                _Receiver.response_codes = [code]
                ok = storefront_cache._deliver_revalidation(
                    url=self.url, secret="s", tags=["catalog-list"],
                    remark="smoke", attempts=4, base_delay=0.01,
                )
                self.assertFalse(ok)
                self.assertEqual(len(_Receiver.captured), 1)

    def test_exhausts_retries_on_persistent_5xx(self):
        _Receiver.response_codes = [503]
        ok = storefront_cache._deliver_revalidation(
            url=self.url, secret="s", tags=["catalog-list"],
            remark="smoke", attempts=3, base_delay=0.01,
        )
        self.assertFalse(ok)
        self.assertEqual(len(_Receiver.captured), 3)  # every attempt made

    def test_retries_connection_errors(self):
        # Transport failures are transient: attempts are all spent, then
        # the exhaustion is reported honestly.
        ok = storefront_cache._deliver_revalidation(
            url="http://127.0.0.1:1/api/revalidate",
            secret="s", tags=["catalog-list"], remark="smoke",
            attempts=2, base_delay=0.01,
        )
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


class TestStatusRecording(unittest.TestCase):
    """The delivery job records its outcome on Settings (ops visibility)."""

    @classmethod
    def setUpClass(cls):
        cls.server, cls.base = _start_receiver()
        cls.url = f"{cls.base}/api/revalidate"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _capture(self):
        calls = []
        return patch.object(
            storefront_cache, "_record_last_status",
            side_effect=lambda msg: calls.append(msg),
        ), calls

    def _receiver(self, codes):
        return patch.object(_Receiver, "response_codes", codes)

    def setUp(self):
        _Receiver.captured = []

    def test_success_message_after_transient(self):
        p1, calls = self._capture()
        with p1, self._receiver([503, 200]):
            ok = storefront_cache._deliver_revalidation(
                url=self.url, secret="s", tags=["a", "b"],
                remark="unit", attempts=3, base_delay=0.01,
            )
        self.assertTrue(ok)
        self.assertEqual(len(calls), 1)
        self.assertIn("delivered 2 tag(s) after 2 attempt(s)", calls[0])
        self.assertIn("(unit)", calls[0])

    def test_success_first_attempt(self):
        p1, calls = self._capture()
        with p1, self._receiver([200]):
            ok = storefront_cache._deliver_revalidation(
                url=self.url, secret="s", tags=["a"],
                remark="unit", attempts=2, base_delay=0.01,
            )
        self.assertTrue(ok)
        self.assertEqual(len(calls), 1)
        self.assertIn("after 1 attempt(s)", calls[0])

    def test_rejection_records_no_retry(self):
        p1, calls = self._capture()
        with p1, self._receiver([401]):
            ok = storefront_cache._deliver_revalidation(
                url=self.url, secret="s", tags=["a"],
                remark="unit", attempts=4, base_delay=0.01,
            )
        self.assertFalse(ok)
        self.assertEqual(len(calls), 1)
        self.assertIn("rejected (no retry)", calls[0])
        self.assertIn("HTTP 401", calls[0])

    def test_exhaustion_records_attempts(self):
        p1, calls = self._capture()
        with p1, self._receiver([503]):
            ok = storefront_cache._deliver_revalidation(
                url=self.url, secret="s", tags=["a"],
                remark="unit", attempts=2, base_delay=0.01,
            )
        self.assertFalse(ok)
        self.assertEqual(len(calls), 1)
        self.assertIn("failed after 2 attempts", calls[0])


class TestCacheBust(unittest.TestCase):
    """Exact key targeting of the bust functions (against frappe.cache()).

    Doc-event *integration* (a Product save actually invoking these) needs
    a bench DB; these unit tests pin the key patterns, which is where a
    silent typo would hide.
    """

    def setUp(self):
        from saathimart.api import storefront_cache as sc
        store = sc.frappe.cache()._store
        store.clear()
        # seed every key family the hub uses
        for key in (
            "sm_product:P1:hub::",
            "sm_product:P1:v1:27.7:85.3:5",
            "sm_best_listing:P1:v1::",
            "sm_best_template:P1::",
            "sm_resolve_listing:P1::",
            "sm_list_products:tea:::1:20:::",
            "sm_list_products:staples:::1:20:::",
            "sm_stock:v1:P1",
            "sm_stock:v2:P2",
            "sm_stock_batch:v1",
            "sm_totals:abc123",
            "sm_totals:def456",
            "sm_brands_list",
            "sm_site_config",          # unrelated — must survive
            "sm_dashboard_summary",    # unrelated — must survive
        ):
            store[key] = "x"

    def _store(self):
        from saathimart.api import storefront_cache as sc
        return sc.frappe.cache()._store

    def test_bust_product_kills_product_and_list_families(self):
        storefront_cache.bust_product_cache("P1")
        store = self._store()
        for gone in (
            "sm_product:P1:hub::", "sm_product:P1:v1:27.7:85.3:5",
            "sm_best_listing:P1:v1::", "sm_best_template:P1::",
            "sm_resolve_listing:P1::",
            "sm_list_products:tea:::1:20:::",   # list cache has no product
            "sm_list_products:staples:::1:20:::",  # name in the key — all go
            "sm_brands_list",
        ):
            self.assertNotIn(gone, store, f"{gone} should be busted")
        # other products' stock + unrelated keys survive
        for kept in ("sm_stock:v1:P1", "sm_site_config", "sm_dashboard_summary"):
            self.assertIn(kept, store, f"{kept} must survive a product bust")

    def test_bust_stock_kills_only_that_vendor_product(self):
        storefront_cache.bust_stock_cache(vendor="v1", product="P1")
        store = self._store()
        self.assertNotIn("sm_stock:v1:P1", store)
        self.assertNotIn("sm_stock_batch:v1", store)
        # other vendor's stock row untouched
        self.assertIn("sm_stock:v2:P2", store)
        # and the product-derived families went too (availability embedded)
        self.assertNotIn("sm_product:P1:hub::", store)
        self.assertNotIn("sm_list_products:tea:::1:20:::", store)

    def test_bust_totals_kills_all_total_keys(self):
        storefront_cache.bust_totals_cache()
        store = self._store()
        self.assertNotIn("sm_totals:abc123", store)
        self.assertNotIn("sm_totals:def456", store)
        self.assertIn("sm_product:P1:hub::", store)  # untouched

    def test_on_coupon_changed_busts_totals(self):
        from types import SimpleNamespace
        doc = SimpleNamespace(doctype="Coupon", name="COUPON-1")
        storefront_cache.on_coupon_changed(doc, "on_update")
        store = self._store()
        self.assertNotIn("sm_totals:abc123", store)

    def test_delete_pattern_survives_old_wrappers(self):
        # A wrapper lacking delete_keys_pattern must not raise out of a bust.
        from saathimart.api import storefront_cache as sc
        real = sc.frappe.cache().delete_keys_pattern
        del type(sc.frappe.cache()).delete_keys_pattern
        try:
            storefront_cache.bust_product_cache("P1")  # must not raise
        finally:
            type(sc.frappe.cache()).delete_keys_pattern = real
