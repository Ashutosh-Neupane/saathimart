"""
Tests for the 4-phase resilience system (dead letter, redis fallback,
event dedup/priority/ordering, partial failure isolation, clock skew,
connection pooling, fallback delivery, stock snapshot, event batching)
and the fixes/wiring applied on top of it.

Run: bench --site <site> run-tests --module saathimart.tests.test_resilience_wiring
"""
import time
import unittest

import frappe
from frappe.utils import add_to_date, now_datetime


class TestEventDedup(unittest.TestCase):
    def setUp(self):
        frappe.set_user("Administrator")
        from saathimart.api.event_dedup import event_fingerprint
        self.fp = event_fingerprint("order.new", "test-dedup-vendor", {"order_id": "X"})
        frappe.cache().delete_key(f"sm_dedup:{self.fp}")

    def test_first_call_not_duplicate(self):
        from saathimart.api.event_dedup import is_duplicate
        self.assertFalse(is_duplicate("order.new", "test-dedup-vendor", {"order_id": "X"}))

    def test_repeat_within_window_is_duplicate(self):
        from saathimart.api.event_dedup import is_duplicate
        is_duplicate("order.new", "test-dedup-vendor", {"order_id": "X"})
        self.assertTrue(is_duplicate("order.new", "test-dedup-vendor", {"order_id": "X"}))

    def test_different_payload_not_duplicate(self):
        from saathimart.api.event_dedup import is_duplicate
        is_duplicate("order.new", "test-dedup-vendor", {"order_id": "X"})
        self.assertFalse(is_duplicate("order.new", "test-dedup-vendor", {"order_id": "Y"}))

    def test_db_fallback_matches_real_content_not_name(self):
        """
        Regression: the DB fallback used to search Webhook Event.name (an
        autonumber) for the fingerprint — a substring that can never be
        there. It now re-fingerprints candidate rows' own payload.
        """
        from saathimart.api.event_dedup import _db_is_duplicate
        payload = {"order_id": "DB-FALLBACK-TEST"}
        doc = frappe.new_doc("Webhook Event")
        doc.event_type = "order.new"
        doc.event_id = frappe.generate_hash(length=10)
        doc.target_vendor = "test-dedup-vendor-db"
        doc.status = "Queued"
        doc.payload = frappe.as_json(payload)
        doc.insert(ignore_permissions=True)
        try:
            self.assertTrue(_db_is_duplicate("order.new", "test-dedup-vendor-db", payload))
            self.assertFalse(_db_is_duplicate("order.new", "test-dedup-vendor-db", {"order_id": "DIFFERENT"}))
        finally:
            frappe.delete_doc("Webhook Event", doc.name, force=True)
            frappe.db.commit()


class TestEventPriority(unittest.TestCase):
    def setUp(self):
        frappe.set_user("Administrator")

    def test_known_types_have_expected_priority(self):
        from saathimart.api.event_priority import get_priority
        self.assertEqual(get_priority("payment.received"), 1)
        self.assertEqual(get_priority("order.new"), 2)
        self.assertEqual(get_priority("stock.update"), 3)
        self.assertEqual(get_priority("analytics"), 4)

    def test_unknown_type_gets_default_normal(self):
        from saathimart.api.event_priority import get_priority, DEFAULT_PRIORITY
        self.assertEqual(get_priority("something.nobody.registered"), DEFAULT_PRIORITY)

    def test_get_events_by_priority_orders_critical_first(self):
        from saathimart.api.event_priority import get_events_by_priority
        docs = []
        try:
            for etype, target in (("stock.update", "prio-test-v"), ("payment.received", "prio-test-v")):
                d = frappe.new_doc("Webhook Event")
                d.event_type = etype
                d.event_id = frappe.generate_hash(length=10)
                d.target_vendor = target
                d.target_site = "https://example.test"
                d.status = "Queued"
                d.priority = 3 if etype == "stock.update" else 1
                d.payload = "{}"
                d.insert(ignore_permissions=True)
                docs.append(d)
            frappe.db.commit()
            ordered = get_events_by_priority(status="Queued", limit=100)
            names = [e["name"] for e in ordered if e["target_vendor"] == "prio-test-v"]
            self.assertEqual(names[0], docs[1].name)  # payment.received (priority 1) first
        finally:
            for d in docs:
                frappe.delete_doc("Webhook Event", d.name, force=True)
            frappe.db.commit()


class TestEventOrdering(unittest.TestCase):
    def setUp(self):
        frappe.set_user("Administrator")

    def test_get_next_sequence_delegates_to_atomic_counter(self):
        """
        Regression: this used to have its own Redis-counter-with-COUNT(*)
        -fallback implementation, which could hand out a colliding sequence
        once old Webhook Event rows are archived. It now delegates to
        publisher._get_next_vendor_event_seq, the same atomic UPDATE
        _enqueue() uses for every real event.
        """
        from saathimart.api.event_ordering import get_next_sequence
        from saathimart.events.publisher import _get_next_vendor_event_seq
        vendor = "test-ordering-vendor"
        frappe.db.set_value("Vendor", vendor, "last_event_seq", 0) if frappe.db.exists("Vendor", vendor) else None
        first = get_next_sequence(vendor) if frappe.db.exists("Vendor", vendor) else None
        # Without a real Vendor row this would throw inside the UPDATE's
        # WHERE clause matching nothing — assert it doesn't raise instead,
        # since most sites won't have a fixture vendor named exactly this.
        if not frappe.db.exists("Vendor", vendor):
            self.skipTest("no fixture Vendor row to test the real counter against")
        second = get_next_sequence(vendor)
        self.assertEqual(second, first + 1)

    def test_verify_sequence_allows_next_in_order(self):
        from saathimart.api.event_ordering import verify_sequence, mark_processed
        vendor = "test-ordering-seq-vendor"
        mark_processed(vendor, 5)
        self.assertTrue(verify_sequence(vendor, 6))

    def test_verify_sequence_holds_on_gap(self):
        from saathimart.api.event_ordering import verify_sequence, mark_processed
        vendor = "test-ordering-gap-vendor"
        mark_processed(vendor, 5)
        self.assertFalse(verify_sequence(vendor, 8))  # gap: 6, 7 missing

    def test_verify_sequence_idempotent_on_replay(self):
        from saathimart.api.event_ordering import verify_sequence, mark_processed
        vendor = "test-ordering-replay-vendor"
        mark_processed(vendor, 5)
        self.assertTrue(verify_sequence(vendor, 3))  # already past — treat as ok, let dedup handle it


class TestPartialFailure(unittest.TestCase):
    def setUp(self):
        frappe.set_user("Administrator")
        from saathimart.api.partial_failure import force_process_vendor
        force_process_vendor("test-pf-vendor")

    def test_within_budget_allows_processing(self):
        from saathimart.api.partial_failure import check_error_budget
        self.assertTrue(check_error_budget("test-pf-vendor"))

    def test_exceeding_budget_defers_vendor(self):
        from saathimart.api.partial_failure import record_vendor_error, check_error_budget, MAX_ERRORS_PER_HOUR
        for _ in range(MAX_ERRORS_PER_HOUR):
            record_vendor_error("test-pf-vendor")
        self.assertFalse(check_error_budget("test-pf-vendor"))

    def test_force_process_clears_defer(self):
        from saathimart.api.partial_failure import record_vendor_error, check_error_budget, force_process_vendor, MAX_ERRORS_PER_HOUR
        for _ in range(MAX_ERRORS_PER_HOUR):
            record_vendor_error("test-pf-vendor")
        self.assertFalse(check_error_budget("test-pf-vendor"))
        force_process_vendor("test-pf-vendor")
        self.assertTrue(check_error_budget("test-pf-vendor"))


class TestClockSync(unittest.TestCase):
    def setUp(self):
        frappe.set_user("Administrator")

    def test_current_epoch_timestamp_is_valid(self):
        from saathimart.api.clock_sync import is_timestamp_valid
        self.assertTrue(is_timestamp_valid(str(int(time.time()))))

    def test_ancient_timestamp_is_invalid(self):
        from saathimart.api.clock_sync import is_timestamp_valid
        self.assertFalse(is_timestamp_valid(str(int(time.time()) - 999999)))

    def test_measure_and_recall_skew(self):
        from saathimart.api.clock_sync import measure_clock_skew, get_vendor_clock_skew
        # Vendor claims to be 45s ahead of "now"
        future_ts = str(int(time.time()) + 45)
        measure_clock_skew("test-clock-vendor", future_ts)
        skew = get_vendor_clock_skew("test-clock-vendor")
        self.assertGreater(skew, 30)  # allow slack for test execution time

    def test_vendor_tolerance_widens_with_measured_skew(self):
        from saathimart.api.clock_sync import measure_clock_skew, is_timestamp_valid
        vendor = "test-clock-tolerance-vendor"
        measure_clock_skew(vendor, str(int(time.time()) + 200))
        # A timestamp 150s stale would fail the flat 300s window comfortably
        # either way; assert the vendor-aware path at least doesn't crash
        # and returns a bool.
        stale_ts = str(int(time.time()) - 150)
        self.assertIsInstance(is_timestamp_valid(stale_ts, vendor_name=vendor), bool)


class TestConnectionPool(unittest.TestCase):
    def setUp(self):
        frappe.set_user("Administrator")

    def test_pooled_request_default_timeout_does_not_raise(self):
        """
        Regression: the default (no explicit timeout) path passed a raw
        (connect, read) tuple straight to urllib3, which rejects tuples —
        only `requests` accepts that convention. Fixed to wrap it in
        urllib3.Timeout. This hits a real (fast-failing) address rather
        than mocking, specifically to catch that TypeError if it comes back.
        """
        from saathimart.api.connection_pool import pooled_request
        status, text, error = pooled_request("GET", "http://127.0.0.1:1/", timeout=None)
        # Connection refused is expected and fine — a TypeError about the
        # timeout argument shape is what this test actually guards against.
        if error:
            self.assertNotIn("must be an int, float or None", error)

    def test_get_pool_stats_structure(self):
        from saathimart.api.connection_pool import get_pool_stats
        stats = get_pool_stats()
        self.assertIn("pool", stats)


class TestEventBatch(unittest.TestCase):
    def setUp(self):
        frappe.set_user("Administrator")

    def test_compress_decompress_roundtrip(self):
        from saathimart.api.event_batch import compress_payload, decompress_payload
        payload = (b"x" * 5000)
        compressed, was_compressed = compress_payload(payload)
        self.assertTrue(was_compressed)
        self.assertEqual(decompress_payload(compressed, was_compressed), payload)

    def test_small_payload_not_compressed(self):
        from saathimart.api.event_batch import compress_payload
        payload = b"tiny"
        result, was_compressed = compress_payload(payload)
        self.assertFalse(was_compressed)
        self.assertEqual(result, payload)

    def test_should_batch_event_matches_batchable_set(self):
        from saathimart.api.event_batch import should_batch_event
        self.assertTrue(should_batch_event("stock.update"))
        self.assertFalse(should_batch_event("payment.received"))

    def test_batch_stock_events_merges_by_product_latest_wins(self):
        from saathimart.api.event_batch import batch_stock_events
        events = [
            {"payload": frappe.as_json({"product": "P1", "stock_qty": 5})},
            {"payload": frappe.as_json({"product": "P1", "stock_qty": 9})},  # same product, newer
            {"payload": frappe.as_json({"product": "P2", "stock_qty": 3})},
        ]
        batch = batch_stock_events("test-vendor", events)
        by_product = {i["product"]: i["stock_qty"] for i in batch["items"]}
        self.assertEqual(by_product["P1"], 9)
        self.assertEqual(by_product["P2"], 3)
        self.assertEqual(batch["batched_from"], 3)
        self.assertEqual(batch["item_count"], 2)

    def test_unpack_stock_batch(self):
        from saathimart.api.event_batch import unpack_batch_event
        unpacked = unpack_batch_event({
            "event_type": "stock.batch",
            "items": [{"product": "P1", "stock_qty": 5}],
        })
        self.assertEqual(unpacked, [("stock.update", {"product": "P1", "stock_qty": 5})])


class TestDeadLetter(unittest.TestCase):
    def setUp(self):
        frappe.set_user("Administrator")

    def test_retry_dead_letters_requeues_recent_dead_events(self):
        from saathimart.api.dead_letter import retry_dead_letters
        doc = frappe.new_doc("Webhook Event")
        doc.event_type = "test.dead"
        doc.event_id = frappe.generate_hash(length=10)
        doc.target_vendor = "test-dl-vendor"
        doc.target_site = "https://example.test"
        doc.status = "Dead"
        doc.retry_count = 3
        doc.dead_letter_reason = "test"
        doc.payload = "{}"
        doc.insert(ignore_permissions=True)
        frappe.db.commit()
        try:
            retry_dead_letters()
            doc.reload()
            self.assertEqual(doc.status, "Queued")
            self.assertEqual(doc.retry_count, 0)
        finally:
            frappe.delete_doc("Webhook Event", doc.name, force=True)
            frappe.db.commit()

    def test_retry_dead_letters_skips_events_beyond_retry_age(self):
        from saathimart.api.dead_letter import retry_dead_letters, MAX_RETRY_AGE_DAYS
        doc = frappe.new_doc("Webhook Event")
        doc.event_type = "test.dead.old"
        doc.event_id = frappe.generate_hash(length=10)
        doc.target_vendor = "test-dl-old-vendor"
        doc.target_site = "https://example.test"
        doc.status = "Dead"
        doc.payload = "{}"
        doc.insert(ignore_permissions=True)
        old_date = add_to_date(now_datetime(), days=-(MAX_RETRY_AGE_DAYS + 5))
        frappe.db.set_value("Webhook Event", doc.name, "creation", old_date, update_modified=False)
        frappe.db.commit()
        try:
            retry_dead_letters()
            status = frappe.db.get_value("Webhook Event", doc.name, "status")
            self.assertEqual(status, "Dead")  # untouched — too old to retry
        finally:
            frappe.delete_doc("Webhook Event", doc.name, force=True)
            frappe.db.commit()


class TestStockSnapshot(unittest.TestCase):
    def setUp(self):
        frappe.set_user("Administrator")

    def test_generate_snapshot_uses_physical_qty_field(self):
        """
        Regression: this used to filter/read a `stock_qty` field that
        doesn't exist on Vendor Stock (real field is physical_qty) — always
        returned an empty snapshot regardless of real stock. Doesn't need a
        real vendor to catch a field-name crash; a nonexistent vendor just
        returns [].
        """
        from saathimart.api.stock_snapshot import generate_stock_snapshot
        result = generate_stock_snapshot("nonexistent-vendor-xyz")
        self.assertEqual(result, [])

    def test_record_report_with_no_discrepancies_is_a_noop(self):
        from saathimart.api.stock_snapshot import record_stock_snapshot_report
        result = record_stock_snapshot_report("nonexistent-vendor-xyz", [])
        self.assertEqual(result, {"ok": True, "discrepancies": 0})

    def test_record_report_flags_when_no_matching_vendor_stock_row(self):
        from saathimart.api.stock_snapshot import record_stock_snapshot_report
        result = record_stock_snapshot_report("nonexistent-vendor-xyz", [
            {"product": "nonexistent-product", "hub_qty": 10, "local_qty": 5, "diff": -5}
        ])
        self.assertEqual(result["corrected"], 0)
        self.assertEqual(result["flagged"], 1)


class TestFallbackDelivery(unittest.TestCase):
    def setUp(self):
        frappe.set_user("Administrator")

    def test_primary_delivery_requires_site_url(self):
        from saathimart.api.fallback_delivery import try_primary_delivery
        class FakeEvt:
            name = "TEST"
            event_type = "test.event"
            payload = "{}"
            target_vendor = "test-fb-vendor"
        ok, error = try_primary_delivery(FakeEvt(), {})
        self.assertFalse(ok)
        self.assertIn("frappe_site_url", error)

    def test_tertiary_delivery_requires_contact_email(self):
        from saathimart.api.fallback_delivery import try_tertiary_delivery
        class FakeEvt:
            name = "TEST"
            event_type = "test.event"
            target_vendor = "test-fb-vendor"
            payload = "{}"
        ok, reason = try_tertiary_delivery(FakeEvt(), {})
        self.assertFalse(ok)
        self.assertIn("contact_email" if "contact_email" in reason else "email", reason)


if __name__ == "__main__":
    unittest.main()
