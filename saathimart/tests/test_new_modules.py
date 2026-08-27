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
    def setUp(self):
        frappe.set_user("Administrator")

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
        from saathimart.api.mobile import get_cart_light
        result = get_cart_light("nonexistent-session")
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


class TestVendorPayout(unittest.TestCase):
    def setUp(self):
        frappe.set_user("Administrator")

    def test_payout_creation(self):
        from saathimart.api.vendor_onboarding import register_vendor
        register_vendor("Test Payout Vendor", "payout@test.com", "9800000003")
        vname = frappe.db.get_value("Vendor", {"vendor_name": "Test Payout Vendor"}, "name")
        if vname:
            vp = frappe.new_doc("Vendor Payout")
            vp.vendor = vname
            vp.period_start = "2026-08-01"
            vp.period_end = "2026-08-31"
            vp.flags.ignore_links = True
            vp.insert(ignore_permissions=True)
            self.assertTrue(vp.name)
            frappe.delete_doc("Vendor Payout", vp.name, force=True)
            frappe.delete_doc("Vendor", vname, force=True)
            frappe.db.commit()


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
