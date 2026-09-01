"""
Tests for all new modules added in the production hardening round.
Run: bench --site <site> run-tests --module saathimart.tests.test_new_modules
"""
import unittest
import frappe
from frappe.utils import flt, now_datetime, add_to_date


class TestRateLimiter(unittest.TestCase):
    def setUp(self):
        frappe.set_user("Administrator")
        from saathimart.api.rate_limiter import clear_failures
        clear_failures("test-ip-123")

    def test_allows_within_threshold(self):
        from saathimart.api.rate_limiter import check_rate_limit, record_failure
        for _ in range(9):
            record_failure("test-ip-123")
        self.assertTrue(check_rate_limit("test-ip-123"))

    def test_blocks_after_threshold(self):
        from saathimart.api.rate_limiter import check_rate_limit, record_failure
        for _ in range(10):
            record_failure("test-ip-123")
        self.assertFalse(check_rate_limit("test-ip-123"))

    def test_clear_resets_count(self):
        from saathimart.api.rate_limiter import record_failure, clear_failures, check_rate_limit
        for _ in range(9):
            record_failure("test-ip-123")
        clear_failures("test-ip-123")
        self.assertTrue(check_rate_limit("test-ip-123"))

    def test_different_ips_independent(self):
        from saathimart.api.rate_limiter import record_failure, check_rate_limit
        for _ in range(10):
            record_failure("ip-a")
        self.assertFalse(check_rate_limit("ip-a"))
        self.assertTrue(check_rate_limit("ip-b"))


class TestCircuitBreaker(unittest.TestCase):
    def setUp(self):
        frappe.set_user("Administrator")
        from saathimart.api.circuit_breaker import reset_circuit
        reset_circuit("test-vendor-cb")

    def test_closed_allows_delivery(self):
        from saathimart.api.circuit_breaker import should_attempt_delivery
        self.assertTrue(should_attempt_delivery("test-vendor-cb"))

    def test_opens_after_failures(self):
        from saathimart.api.circuit_breaker import record_delivery_failure, should_attempt_delivery
        for _ in range(5):
            record_delivery_failure("test-vendor-cb")
        self.assertFalse(should_attempt_delivery("test-vendor-cb"))

    def test_success_resets_count(self):
        from saathimart.api.circuit_breaker import record_delivery_failure, record_delivery_success
        for _ in range(4):
            record_delivery_failure("test-vendor-cb")
        record_delivery_success("test-vendor-cb")
        from saathimart.api.circuit_breaker import should_attempt_delivery
        self.assertTrue(should_attempt_delivery("test-vendor-cb"))

    def test_get_state(self):
        from saathimart.api.circuit_breaker import get_circuit_state
        state = get_circuit_state("nonexistent-vendor")
        self.assertEqual(state["state"], "closed")


class TestLoyaltyEnhanced(unittest.TestCase):
    """
    earn_points/redeem_points/apply_referral/check_birthday_rewards had zero
    coverage before this — every real insert() call was wrapped in
    try/except: log_error, so a systemic failure (a missing mandatory
    `program` field, in this case — every single insert failed, silently,
    always) never surfaced as a test failure. These exercise the actual
    DB-writing paths, not just the pure calculation helpers above.
    """

    TEST_PROGRAM = "Test Rewards Enhanced Coverage"

    def setUp(self):
        frappe.set_user("Administrator")
        if not frappe.db.exists("Loyalty Program", self.TEST_PROGRAM):
            frappe.get_doc({
                "doctype": "Loyalty Program",
                "program_name": self.TEST_PROGRAM,
                "is_active": 1,
            }).insert(ignore_permissions=True)
        s = frappe.get_single("Settings")
        self._orig_enable = s.enable_loyalty
        self._orig_program = s.loyalty_program
        s.enable_loyalty = 1
        s.loyalty_program = self.TEST_PROGRAM
        s.save(ignore_permissions=True)
        frappe.db.commit()

    def tearDown(self):
        rows = frappe.get_all(
            "Loyalty Point Entry",
            filters={"customer_email": ["like", "%enhanced-coverage%"]},
            pluck="name",
        )
        for r in rows:
            frappe.delete_doc("Loyalty Point Entry", r, force=True, ignore_permissions=True)
        s = frappe.get_single("Settings")
        s.enable_loyalty = self._orig_enable
        s.loyalty_program = self._orig_program
        s.save(ignore_permissions=True)
        frappe.db.commit()

    def test_earn_points_persists_with_valid_program_and_entry_type(self):
        """
        Regression: program was never set (mandatory field) and entry_type
        was "Earn" against a doctype whose real Select options are
        "Earned"/"Redeemed"/... — every insert failed.
        """
        from saathimart.api.loyalty_enhanced import earn_points
        email = "enhanced-coverage-earn@test.np"
        result = earn_points(email, None, 1000)
        self.assertGreater(result, 0)
        rows = frappe.get_all(
            "Loyalty Point Entry", filters={"customer_email": email},
            fields=["entry_type", "program", "source", "tier"],
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["entry_type"], "Earned")
        self.assertEqual(rows[0]["program"], self.TEST_PROGRAM)
        self.assertEqual(rows[0]["source"], "order")

    def test_redeem_points_persists_and_deducts_balance(self):
        from saathimart.api.loyalty_enhanced import earn_points, redeem_points, get_points_balance
        email = "enhanced-coverage-redeem@test.np"
        earn_points(email, None, 1000)
        before = get_points_balance(email)
        redeem_points(email, None, 5)
        after = get_points_balance(email)
        self.assertEqual(after, before - 5)

    def test_apply_referral_is_idempotent(self):
        """
        Regression: the dedup check filtered on `order` containing the new
        customer's email — but `order` is a strict Link to Order, so the
        write that check was meant to detect (a synthetic "referral:<email>"
        string) never actually succeeded either; both the write and its own
        duplicate guard were broken. Now uses `remarks`, a real free-text field.
        """
        from saathimart.api.loyalty_enhanced import apply_referral, get_points_balance
        referrer = "enhanced-coverage-referrer@test.np"
        apply_referral(referrer, "enhanced-coverage-referred@test.np")
        apply_referral(referrer, "enhanced-coverage-referred@test.np")
        rows = frappe.get_all("Loyalty Point Entry", filters={"customer_email": referrer})
        self.assertEqual(len(rows), 1)
        self.assertEqual(get_points_balance(referrer), 100)

    def test_tier_bronze(self):
        from saathimart.api.loyalty_enhanced import get_customer_tier
        tier = get_customer_tier("new-customer@test.com")
        self.assertEqual(tier, "Bronze")

    def test_earn_points_calculation(self):
        from saathimart.api.loyalty_enhanced import calculate_earn_points
        # Bronze: 1% earn rate, Rs 1000 order = 10 points
        points = calculate_earn_points(1000, customer_email="new-customer@test.com")
        self.assertEqual(points, 10)

    def test_points_balance(self):
        from saathimart.api.loyalty_enhanced import get_points_balance
        balance = get_points_balance("nonexistent@test.com")
        self.assertEqual(balance, 0)

    def test_dashboard_structure(self):
        from saathimart.api.loyalty_enhanced import get_loyalty_dashboard
        dashboard = get_loyalty_dashboard("test@test.com")
        self.assertIn("tier", dashboard)
        self.assertIn("balance", dashboard)
        self.assertIn("earn_rate", dashboard)
        self.assertIn("history", dashboard)


class TestVendorPerformance(unittest.TestCase):
    def setUp(self):
        frappe.set_user("Administrator")

    def test_scorecard_structure(self):
        from saathimart.api.vendor_performance import get_vendor_scorecard
        scorecard = get_vendor_scorecard("nonexistent-vendor", days=30)
        self.assertIn("delivery_rate", scorecard)
        self.assertIn("overall_score", scorecard)
        self.assertIn("total_orders", scorecard)
        self.assertEqual(scorecard["total_orders"], 0)

    def test_all_vendor_scores(self):
        from saathimart.api.vendor_performance import get_all_vendor_scores
        scores = get_all_vendor_scores(days=30)
        self.assertIsInstance(scores, list)


class TestSearch(unittest.TestCase):
    def setUp(self):
        frappe.set_user("Administrator")

    def test_empty_query_returns_empty(self):
        from saathimart.api.search import search_products
        result = search_products(query="")
        self.assertIn("results", result)
        self.assertIn("total", result)

    def test_suggestions_short_query(self):
        from saathimart.api.search import search_suggestions
        result = search_suggestions(query="a")
        self.assertEqual(result, [])

    def test_suggestions_returns_list(self):
        from saathimart.api.search import search_suggestions
        result = search_suggestions(query="xyz123nonexistent")
        self.assertIsInstance(result, list)


class TestVendorOnboarding(unittest.TestCase):
    def setUp(self):
        frappe.set_user("Administrator")

    def test_register_vendor(self):
        from saathimart.api.vendor_onboarding import register_vendor
        result = register_vendor(
            vendor_name="Test Onboard Vendor",
            contact_email="onboard@test.com",
            contact_phone="9800000000",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "Pending")
        # Cleanup
        if frappe.db.exists("Vendor", {"vendor_name": "Test Onboard Vendor"}):
            name = frappe.db.get_value("Vendor", {"vendor_name": "Test Onboard Vendor"}, "name")
            frappe.delete_doc("Vendor", name, force=True)
            frappe.db.commit()

    def test_duplicate_registration_fails(self):
        from saathimart.api.vendor_onboarding import register_vendor
        # Create one first
        register_vendor("Test Dup Vendor", "dup@test.com", "9800000001")
        with self.assertRaises(frappe.ValidationError):
            register_vendor("Test Dup Vendor", "dup2@test.com", "9800000002")
        # Cleanup
        name = frappe.db.get_value("Vendor", {"vendor_name": "Test Dup Vendor"}, "name")
        if name:
            frappe.delete_doc("Vendor", name, force=True)
            frappe.db.commit()

    def test_onboarding_status(self):
        from saathimart.api.vendor_onboarding import get_onboarding_status
        result = get_onboarding_status("nonexistent")
        self.assertEqual(result["status"], "not_found")


class TestOrderEventSourcing(unittest.TestCase):
    def setUp(self):
        frappe.set_user("Administrator")

    def test_record_event(self):
        from saathimart.api.order_events import record_order_event
        record_order_event("TEST-ORDER-001", "created", {"customer": "Test"})
        # Should not raise

    def test_get_timeline(self):
        from saathimart.api.order_events import get_order_timeline
        timeline = get_order_timeline("nonexistent-order")
        self.assertEqual(timeline, [])

    def test_describe_event(self):
        from saathimart.api.order_events import _describe_event
        desc = _describe_event("paid", {"amount": 500})
        self.assertIn("500", desc)


class TestNotifications(unittest.TestCase):
    def setUp(self):
        frappe.set_user("Administrator")

    def test_send_payment_confirmation(self):
        from saathimart.api.notifications import send_payment_confirmation
        # Should not raise even without email server
        send_payment_confirmation("test@test.com", "TEST-001", 500, [{"product_name": "Rice", "qty": 2, "rate": 250}])

    def test_send_dispatch_notification(self):
        from saathimart.api.notifications import send_dispatch_notification
        send_dispatch_notification("test@test.com", "TEST-001", "Vendor 1")


class TestImages(unittest.TestCase):
    def setUp(self):
        frappe.set_user("Administrator")

    def test_get_image_url(self):
        from saathimart.api.images import get_image_url
        url = get_image_url("/files/test.jpg")
        self.assertEqual(url, "/files/test.jpg")

    def test_get_image_url_external(self):
        from saathimart.api.images import get_image_url
        url = get_image_url("https://example.com/img.jpg")
        self.assertEqual(url, "https://example.com/img.jpg")

    def test_get_image_url_empty(self):
        from saathimart.api.images import get_image_url
        url = get_image_url("")
        self.assertEqual(url, "")

    def test_get_thumbnail_url(self):
        from saathimart.api.images import get_thumbnail_url
        url = get_thumbnail_url("/files/test.jpg", "small")
        # Should return the original if thumbnail doesn't exist
        self.assertIn("test.jpg", url)


class TestMobile(unittest.TestCase):
    def setUp(self):
        frappe.set_user("Administrator")

    def test_list_products_light(self):
        from saathimart.api.mobile import list_products_light
        result = list_products_light()
        self.assertIn("products", result)
        self.assertIn("total", result)
        self.assertIn("has_more", result)

    def test_get_cart_light_empty(self):
        """
        find_active_cart() (see cart.py) deliberately prefers the signed-in
        user's own cart over the session_id argument — right for a real
        request, but it means this must run as Guest: "Administrator" is
        the user nearly every other test in the suite runs as too, so an
        Active cart genuinely left over from another test would make this
        depend on run order instead of testing what it says it tests.
        """
        from saathimart.api.mobile import get_cart_light
        frappe.set_user("Guest")
        try:
            result = get_cart_light("nonexistent-session")
        finally:
            frappe.set_user("Administrator")
        self.assertEqual(result["items"], [])
        self.assertEqual(result["total"], 0)


class TestAnalytics(unittest.TestCase):
    def setUp(self):
        frappe.set_user("Administrator")

    def test_dashboard_summary(self):
        from saathimart.api.analytics import get_dashboard_summary
        result = get_dashboard_summary(days=30)
        self.assertIn("total_orders", result)
        self.assertIn("total_revenue", result)
        self.assertIn("top_products", result)
        self.assertIn("daily_revenue", result)

    def test_vendor_analytics(self):
        from saathimart.api.analytics import get_vendor_analytics
        result = get_vendor_analytics("nonexistent-vendor", days=30)
        self.assertEqual(result["total_orders"], 0)

    def test_product_analytics(self):
        from saathimart.api.analytics import get_product_analytics
        result = get_product_analytics("nonexistent-product", days=30)
        self.assertEqual(result["total_sold"], 0)


class TestCache(unittest.TestCase):
    def setUp(self):
        frappe.set_user("Administrator")

    def test_cached_decorator(self):
        from saathimart.api.cache import cached
        call_count = [0]

        @cached(prefix="test_cache:", ttl=60)
        def my_func(x):
            call_count[0] += 1
            return x * 2

        result1 = my_func(5)
        result2 = my_func(5)
        self.assertEqual(result1, 10)
        self.assertEqual(result2, 10)
        # Should only call once (cached)
        self.assertEqual(call_count[0], 1)

    def test_invalidate_stock(self):
        from saathimart.api.cache import invalidate_stock
        # Should not raise
        invalidate_stock(vendor="test", product="test")


if __name__ == "__main__":
    unittest.main()
