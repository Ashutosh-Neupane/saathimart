"""
SaathiMart test suite.

Covers:
  - Product price resolution (multi-tier)
  - Cart CRUD
  - Checkout + calculate_taxes_and_totals
  - Coupon validation
  - Loyalty earn / redeem / balance
  - eSewa signature verification
  - Order status transitions
  - API auth guards

Run:
    bench --site <site> run-tests --app saathimart
"""
import json
import unittest

import frappe
from frappe.utils import flt, today, add_days


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_product(name, price=100, stock=50, prices=None):
    slug = frappe.scrub(name).replace("_", "-")
    if frappe.db.exists("Product", {"slug": slug}):
        existing = frappe.get_doc("Product", {"slug": slug})
        frappe.db.sql("DELETE FROM `tabVendor Listing` WHERE product = %s", existing.name)
        frappe.db.sql("DELETE FROM `tabVendor Stock` WHERE product = %s", existing.name)
        frappe.db.sql("DELETE FROM `tabProduct Price` WHERE parent = %s", existing.name)
        frappe.db.delete("Product", existing.name)
    doc = frappe.new_doc("Product")
    doc.product_name   = name
    doc.status         = "Active"
    for p in (prices or []):
        doc.append("prices", p)
    doc.insert(ignore_permissions=True)

    vendor = _make_vendor(f"Test Vendor {name}")
    from saathimart.api.stock import get_or_create
    row = get_or_create(vendor.name, doc.name)
    frappe.db.set_value("Vendor Stock", row.name, {
        "available_qty": stock,
        "reserved_qty": 0,
        "physical_qty": stock,
    })

    base_listing = frappe.new_doc("Vendor Listing")
    base_listing.vendor = vendor.name
    base_listing.product = doc.name
    base_listing.price = price
    base_listing.compare_price = 0
    base_listing.track_inventory = 1
    base_listing.allow_backorder = 0
    base_listing.available_qty = stock
    base_listing.reserved_qty = 0
    base_listing.physical_qty = stock
    base_listing.priority = 1
    base_listing.estimated_delivery_minutes = 20
    base_listing.status = "Active"
    base_listing.insert(ignore_permissions=True)

    from saathimart.api.stock import get_or_create
    for v in [vendor.name] + [p["vendor"] for p in (prices or []) if p.get("vendor")]:
        row = get_or_create(v, doc.name)
        frappe.db.set_value("Vendor Stock", row.name, {
            "available_qty": stock,
            "reserved_qty": 0,
            "physical_qty": stock,
        })

    for p in (prices or []):
        if p.get("vendor"):
            vl = frappe.new_doc("Vendor Listing")
            vl.vendor = p["vendor"]
            vl.product = doc.name
            vl.price = flt(p.get("price") or 0)
            vl.compare_price = flt(p.get("compare_price") or 0)
            vl.track_inventory = 1
            vl.allow_backorder = 0
            vl.available_qty = stock
            vl.reserved_qty = 0
            vl.physical_qty = stock
            vl.delivery_zone = p.get("delivery_zone") or ""
            vl.priority = 1
            vl.estimated_delivery_minutes = 20
            vl.status = "Active"
            vl.insert(ignore_permissions=True)

    return doc


def _make_zone(name, charge=80, free_above=1500):
    if frappe.db.exists("Delivery Zone", name):
        return frappe.get_doc("Delivery Zone", name)
    doc = frappe.new_doc("Delivery Zone")
    doc.zone_name          = name
    doc.delivery_charge    = charge
    doc.free_delivery_above = free_above
    doc.is_active          = 1
    doc.insert(ignore_permissions=True)
    return doc


def _make_cart(session_id="test-session-001"):
    from saathimart.api.cart import _get_or_create_cart
    frappe.set_user("Guest")
    return _get_or_create_cart(session_id)


def _make_vendor(name, slug=None, zone=None):
    if frappe.db.exists("Vendor", {"vendor_name": name}):
        return frappe.get_doc("Vendor", {"vendor_name": name})
    doc = frappe.new_doc("Vendor")
    doc.vendor_name = name
    doc.slug = slug or frappe.scrub(name).replace("_", "-")
    doc.status = "Active"
    if zone:
        doc.delivery_zone = zone
    doc.insert(ignore_permissions=True)
    return doc


def _make_vendor_user(email, vendor_name):
    """Create (or reuse) a User with the SM Vendor role, linked to `vendor_name`
    via Vendor.contact_email — used to exercise role-filtered API paths like
    list_orders() under a real non-admin session instead of mocking roles."""
    if not frappe.db.exists("Role", "SM Vendor"):
        role = frappe.new_doc("Role")
        role.role_name = "SM Vendor"
        role.insert(ignore_permissions=True)

    if frappe.db.exists("User", email):
        user = frappe.get_doc("User", email)
    else:
        user = frappe.new_doc("User")
        user.email = email
        user.first_name = "Test Vendor User"
        user.send_welcome_email = 0
        user.insert(ignore_permissions=True)

    if not any(r.role == "SM Vendor" for r in user.roles):
        user.append("roles", {"role": "SM Vendor"})
        user.save(ignore_permissions=True)

    frappe.db.set_value("Vendor", vendor_name, "contact_email", email)
    return user


def _seed_vendor_stock(vendor, product, available=100, reserved=0):
    from saathimart.api.stock import get_or_create
    row = get_or_create(vendor, product)
    frappe.db.set_value("Vendor Stock", row.name, {
        "available_qty": available,
        "reserved_qty": reserved,
        "physical_qty": available + reserved,
    })
    return row.name


def _make_coupon(code, coupon_type="Percentage", pct=10, amount=0,
                 min_order=0, max_uses=0, valid_days=30):
    if frappe.db.exists("Coupon", code):
        frappe.delete_doc("Coupon", code, ignore_permissions=True)
    doc = frappe.new_doc("Coupon")
    doc.coupon_code          = code
    doc.coupon_type          = coupon_type
    doc.discount_percentage  = pct
    doc.discount_amount      = amount
    doc.min_order_amount     = min_order
    doc.max_uses             = max_uses
    doc.is_active            = 1
    doc.valid_from           = today()
    doc.valid_to             = add_days(today(), valid_days)
    doc.insert(ignore_permissions=True)
    return doc


# ── Test: Product price resolution ───────────────────────────────────────────

class TestProductPricing(unittest.TestCase):

    def setUp(self):
        frappe.set_user("Administrator")
        self.zone = _make_zone("Test Zone KTM", charge=80, free_above=1500)
        self.product = _make_product("Test Tomato", price=80, prices=[
            {"price_type": "Retail",    "price": 80,  "min_qty": 1,  "is_active": 1},
            {"price_type": "Wholesale", "price": 65,  "min_qty": 10, "is_active": 1},
            {"price_type": "B2B",       "price": 55,  "min_qty": 50, "is_active": 1},
            {"price_type": "Zone",      "price": 75,  "min_qty": 1,
             "delivery_zone": self.zone.name, "is_active": 1},
        ])

    def test_retail_price_single_qty(self):
        from saathimart.saathimart.doctype.product.product import get_effective_price
        price = get_effective_price(self.product, price_type="Retail", qty=1)
        self.assertEqual(price, 80)

    def test_wholesale_price_at_min_qty(self):
        from saathimart.saathimart.doctype.product.product import get_effective_price
        price = get_effective_price(self.product, price_type="Wholesale", qty=10)
        self.assertEqual(price, 65)

    def test_wholesale_below_min_qty_falls_back_to_retail(self):
        from saathimart.saathimart.doctype.product.product import get_effective_price
        price = get_effective_price(self.product, price_type="Wholesale", qty=5)
        # min_qty=10 not met → falls back to Retail
        self.assertEqual(price, 80)

    def test_b2b_price(self):
        from saathimart.saathimart.doctype.product.product import get_effective_price
        price = get_effective_price(self.product, price_type="B2B", qty=50)
        self.assertEqual(price, 55)

    def test_zone_specific_price(self):
        from saathimart.saathimart.doctype.product.product import get_effective_price
        price = get_effective_price(
            self.product, price_type="Zone", qty=1, delivery_zone=self.zone.name
        )
        self.assertEqual(price, 75)

    def test_base_price_fallback(self):
        from saathimart.saathimart.doctype.product.product import get_effective_price
        # Request a type with no matching row → falls back to base price field
        price = get_effective_price(self.product, price_type="Member", qty=1)
        self.assertEqual(price, 80)  # base price field


# ── Test: Vendor (Site Price) pricing ──────────────────────────────────────────
# Multi-vendor model: the same Product is sold by several vendor sites at their
# own Site Price, instead of a single Retail/Wholesale tier. See Product Price
# child table (vendor field) and get_effective_price() resolution order.

class TestVendorPricing(unittest.TestCase):

    def setUp(self):
        frappe.set_user("Administrator")
        self.zone = _make_zone("Vendor Pricing Zone", charge=50, free_above=1000)
        self.vendor_a = _make_vendor("Vendor Pricing Test A", slug="vendor-pricing-test-a")
        self.vendor_b = _make_vendor("Vendor Pricing Test B", slug="vendor-pricing-test-b")
        self.product = _make_product("Vendor Pricing Product", price=80, prices=[
            {"price_type": "Retail", "price": 80, "min_qty": 1, "is_active": 1},
            {"price_type": "Site Price", "vendor": self.vendor_a.name,
             "price": 90, "min_qty": 1, "is_active": 1},
            {"price_type": "Site Price", "vendor": self.vendor_b.name,
             "price": 95, "min_qty": 1, "is_active": 1},
            {"price_type": "Site Price", "vendor": self.vendor_a.name,
             "delivery_zone": self.zone.name, "price": 85, "min_qty": 1, "is_active": 1},
        ])

    def test_vendor_a_gets_site_price(self):
        from saathimart.saathimart.doctype.product.product import get_effective_price
        price = get_effective_price(self.product, vendor=self.vendor_a.name)
        self.assertEqual(price, 90)

    def test_vendor_b_gets_different_site_price(self):
        from saathimart.saathimart.doctype.product.product import get_effective_price
        price = get_effective_price(self.product, vendor=self.vendor_b.name)
        self.assertEqual(price, 95)

    def test_no_vendor_falls_back_to_hub_retail(self):
        from saathimart.saathimart.doctype.product.product import get_effective_price
        price = get_effective_price(self.product)
        self.assertEqual(price, 80)

    def test_vendor_zone_price_beats_vendor_base(self):
        from saathimart.saathimart.doctype.product.product import get_effective_price
        price = get_effective_price(
            self.product, vendor=self.vendor_a.name, delivery_zone=self.zone.name
        )
        self.assertEqual(price, 85)

    def test_unknown_vendor_falls_back_to_retail(self):
        from saathimart.saathimart.doctype.product.product import get_effective_price
        price = get_effective_price(self.product, vendor="not-a-real-vendor")
        self.assertEqual(price, 80)

    def test_price_update_event_creates_new_vendor_price_row(self):
        from saathimart.api.events import _apply_price_update
        from saathimart.saathimart.doctype.product.product import get_effective_price

        vendor_c = _make_vendor("Vendor Pricing Test C", slug="vendor-pricing-test-c")
        _apply_price_update({
            "product_id": self.product.name,
            "vendor": vendor_c.name,
            "price": 99,
        })
        doc = frappe.get_doc("Product", self.product.name)
        price = get_effective_price(doc, vendor=vendor_c.name)
        self.assertEqual(price, 99)

    def test_price_update_event_updates_existing_vendor_price_row(self):
        from saathimart.api.events import _apply_price_update
        from saathimart.saathimart.doctype.product.product import get_effective_price

        _apply_price_update({
            "product_id": self.product.name,
            "vendor": self.vendor_a.name,
            "price": 120,
        })
        doc = frappe.get_doc("Product", self.product.name)
        price = get_effective_price(doc, vendor=self.vendor_a.name)
        self.assertEqual(price, 120)
        # Row count for vendor_a (no zone) should still be 1 — updated, not duplicated
        rows = [r for r in doc.prices
                if r.vendor == self.vendor_a.name and not r.delivery_zone]
        self.assertEqual(len(rows), 1)

    def test_price_update_event_ignores_missing_vendor(self):
        from saathimart.api.events import _apply_price_update
        from saathimart.saathimart.doctype.product.product import get_effective_price

        _apply_price_update({
            "product_id": self.product.name,
            "vendor": "",
            "price": 999,
        })
        doc = frappe.get_doc("Product", self.product.name)
        # No vendor supplied → nothing changed, base retail price still resolves
        price = get_effective_price(doc)
        self.assertEqual(price, 80)

    def test_list_products_with_vendor_returns_vendor_price(self):
        from saathimart.api.products import list_products
        result = list_products(vendor=self.vendor_a.name, search="Vendor Pricing Product")
        item = next(i for i in result["items"] if i["name"] == self.product.name)
        self.assertEqual(item["price"], 90)

    def test_list_products_without_vendor_returns_base_price(self):
        from saathimart.api.products import list_products
        result = list_products(search="Vendor Pricing Product")
        item = next(i for i in result["items"] if i["name"] == self.product.name)
        self.assertEqual(item["price"], 80)

    def test_get_product_with_vendor_returns_vendor_price(self):
        from saathimart.api.products import get_product
        frappe.cache().delete_key(f"sm_product:{self.product.name}:{self.vendor_b.name}")
        data = get_product(self.product.slug, vendor=self.vendor_b.name)
        self.assertEqual(data["price"], 95)
        self.assertEqual(data["vendor_context"], self.vendor_b.name)

    def test_cart_item_captures_vendor_and_resolves_site_price(self):
        from saathimart.api.cart import add_to_cart, get_cart
        session = "vendor-pricing-cart-session"
        name = frappe.db.get_value("Cart", {"session_id": session}, "name")
        if name:
            frappe.delete_doc("Cart", name, ignore_permissions=True)
        frappe.set_user("Guest")

        add_to_cart(session, self.product.name, qty=1, vendor=self.vendor_a.name)
        cart = get_cart(session)
        self.assertEqual(cart["items"][0]["vendor"], self.vendor_a.name)
        self.assertEqual(cart["items"][0]["rate"], 90)

    def test_same_product_different_vendor_are_separate_cart_lines(self):
        from saathimart.api.cart import add_to_cart, get_cart
        session = "vendor-pricing-cart-session-2"
        name = frappe.db.get_value("Cart", {"session_id": session}, "name")
        if name:
            frappe.delete_doc("Cart", name, ignore_permissions=True)
        frappe.set_user("Guest")

        add_to_cart(session, self.product.name, qty=1, vendor=self.vendor_a.name)
        add_to_cart(session, self.product.name, qty=1, vendor=self.vendor_b.name)
        cart = get_cart(session)
        self.assertEqual(len(cart["items"]), 2)
        rates = sorted(i["rate"] for i in cart["items"])
        self.assertEqual(rates, [90, 95])

    def test_totals_uses_vendor_price_per_item(self):
        from saathimart.api.totals import preview_order_totals
        result = preview_order_totals(
            items=[{"product": self.product.name, "qty": 1, "vendor": self.vendor_a.name}],
        )
        self.assertEqual(result["items"][0]["rate"], 90)
        self.assertEqual(result["net_total"], 90)

    def test_totals_differ_by_vendor_for_same_product(self):
        from saathimart.api.totals import preview_order_totals
        result_a = preview_order_totals(
            items=[{"product": self.product.name, "qty": 1, "vendor": self.vendor_a.name}],
        )
        result_b = preview_order_totals(
            items=[{"product": self.product.name, "qty": 1, "vendor": self.vendor_b.name}],
        )
        self.assertNotEqual(result_a["net_total"], result_b["net_total"])
        self.assertEqual(result_a["net_total"], 90)
        self.assertEqual(result_b["net_total"], 95)


# ── Test: calculate_taxes_and_totals ─────────────────────────────────────────

class TestTotalsEngine(unittest.TestCase):

    def setUp(self):
        frappe.set_user("Administrator")
        _make_product("Totals Product A", price=500)
        _make_product("Totals Product B", price=300)

    def _base_order(self):
        return {
            "items": [
                {"product": "Totals Product A", "qty": 2, "rate": 500, "amount": 0},
                {"product": "Totals Product B", "qty": 1, "rate": 300, "amount": 0},
            ],
            "delivery_charge": 80,
            "discount_amount": 0,
            "coupon_code": "",
            "loyalty_points_redeemed": 0,
            "customer_email": "test@example.com",
            "taxes": [],
        }

    def test_basic_totals(self):
        from saathimart.api.totals import calculate_taxes_and_totals
        order = self._base_order()
        calculate_taxes_and_totals(order)
        # subtotal = 2×500 + 1×300 = 1300
        self.assertEqual(order["net_total"], 1300)
        # grand_total = 1300 + 80 delivery = 1380
        self.assertEqual(order["grand_total"], 1380)

    def test_item_amounts_set(self):
        from saathimart.api.totals import calculate_taxes_and_totals
        order = self._base_order()
        calculate_taxes_and_totals(order)
        self.assertEqual(order["items"][0]["amount"], 1000)
        self.assertEqual(order["items"][1]["amount"], 300)

    def test_tax_on_net_total(self):
        from saathimart.api.totals import calculate_taxes_and_totals
        order = self._base_order()
        order["taxes"] = [{"charge_type": "On Net Total", "rate": 13, "tax_amount": 0}]
        calculate_taxes_and_totals(order)
        # VAT 13% on 1300 = 169
        self.assertEqual(order["total_taxes"], 169)
        self.assertEqual(order["grand_total"], 1300 + 169 + 80)

    def test_manual_discount(self):
        from saathimart.api.totals import calculate_taxes_and_totals
        order = self._base_order()
        order["discount_amount"] = 100
        calculate_taxes_and_totals(order)
        self.assertEqual(order["grand_total"], 1300 + 80 - 100)

    def test_grand_total_never_negative(self):
        from saathimart.api.totals import calculate_taxes_and_totals
        order = self._base_order()
        order["discount_amount"] = 99999
        calculate_taxes_and_totals(order)
        self.assertGreaterEqual(order["grand_total"], 0)

    def test_free_delivery_threshold(self):
        """Delivery charge should be 0 when subtotal >= free_delivery_above."""
        from saathimart.api.totals import calculate_taxes_and_totals
        order = self._base_order()
        order["delivery_charge"] = 0  # caller already resolved this
        calculate_taxes_and_totals(order)
        self.assertEqual(order["grand_total"], 1300)


# ── Test: Coupon ──────────────────────────────────────────────────────────────

class TestCoupon(unittest.TestCase):

    def setUp(self):
        frappe.set_user("Administrator")

    def test_percentage_coupon(self):
        from saathimart.saathimart.doctype.coupon.coupon import validate_coupon
        _make_coupon("TEST-PCT10", coupon_type="Percentage", pct=10, min_order=0)
        result = validate_coupon("TEST-PCT10", 1000)
        self.assertEqual(result["discount"], 100)

    def test_fixed_coupon(self):
        from saathimart.saathimart.doctype.coupon.coupon import validate_coupon
        _make_coupon("TEST-FLAT50", coupon_type="Fixed Amount", amount=50, min_order=0)
        result = validate_coupon("TEST-FLAT50", 500)
        self.assertEqual(result["discount"], 50)

    def test_fixed_coupon_capped_at_subtotal(self):
        from saathimart.saathimart.doctype.coupon.coupon import validate_coupon
        _make_coupon("TEST-FLAT999", coupon_type="Fixed Amount", amount=999, min_order=0)
        result = validate_coupon("TEST-FLAT999", 200)
        self.assertEqual(result["discount"], 200)  # capped at subtotal

    def test_min_order_not_met_raises(self):
        from saathimart.saathimart.doctype.coupon.coupon import validate_coupon
        _make_coupon("TEST-MINORD", coupon_type="Percentage", pct=10, min_order=1000)
        with self.assertRaises(frappe.ValidationError):
            validate_coupon("TEST-MINORD", 500)

    def test_expired_coupon_raises(self):
        from saathimart.saathimart.doctype.coupon.coupon import validate_coupon
        _make_coupon("TEST-EXPIRED", valid_days=-1)  # already expired
        with self.assertRaises(frappe.ValidationError):
            validate_coupon("TEST-EXPIRED", 500)

    def test_invalid_code_raises(self):
        from saathimart.saathimart.doctype.coupon.coupon import validate_coupon
        with self.assertRaises(frappe.ValidationError):
            validate_coupon("DOES-NOT-EXIST", 500)


# ── Test: Loyalty ─────────────────────────────────────────────────────────────

class TestLoyalty(unittest.TestCase):

    TEST_EMAIL = "loyalty_test@saathimart.np"

    def setUp(self):
        frappe.set_user("Administrator")
        # Ensure settings has loyalty enabled with a program
        if not frappe.db.exists("Loyalty Program", "Test Rewards"):
            prog = frappe.new_doc("Loyalty Program")
            prog.program_name                 = "Test Rewards"
            prog.is_active                    = 1
            prog.collection_factor            = 0.01
            prog.redemption_factor            = 1.0
            prog.min_points_to_redeem         = 50
            prog.max_redemption_per_order_pct = 20
            prog.point_expiry_days            = 365
            prog.insert(ignore_permissions=True)

        s = frappe.get_single("Settings")
        s.enable_loyalty   = 1
        s.loyalty_program  = "Test Rewards"
        s.save(ignore_permissions=True)

        # Clear existing entries for test email
        frappe.db.delete("Loyalty Point Entry", {"customer_email": self.TEST_EMAIL})
        frappe.db.commit()

    def test_initial_balance_zero(self):
        from saathimart.api.loyalty import get_balance
        self.assertEqual(get_balance(self.TEST_EMAIL), 0)

    def test_earn_points(self):
        from saathimart.api.loyalty import earn_points, get_balance
        earned = earn_points(self.TEST_EMAIL, "TEST-ORD-001", 1000)
        # 1000 × 0.01 = 10 points
        self.assertEqual(earned, 10)
        self.assertEqual(get_balance(self.TEST_EMAIL), 10)

    def test_earn_multiple_orders(self):
        from saathimart.api.loyalty import earn_points, get_balance
        earn_points(self.TEST_EMAIL, "TEST-ORD-002", 2000)
        earn_points(self.TEST_EMAIL, "TEST-ORD-003", 3000)
        # 20 + 30 = 50
        self.assertEqual(get_balance(self.TEST_EMAIL), 50)

    def test_redemption_discount(self):
        from saathimart.api.loyalty import earn_points, calculate_redemption_discount
        earn_points(self.TEST_EMAIL, "TEST-ORD-004", 10000)  # 100 points
        result = calculate_redemption_discount(self.TEST_EMAIL, 50, 1000)
        self.assertTrue(result["ok"])
        # 50 points × NPR 1 = NPR 50, but capped at 20% of 1000 = 200 → NPR 50
        self.assertEqual(result["discount"], 50)

    def test_redemption_capped_by_order_pct(self):
        from saathimart.api.loyalty import earn_points, calculate_redemption_discount
        earn_points(self.TEST_EMAIL, "TEST-ORD-005", 100000)  # 1000 points
        # Try to redeem 500 points = NPR 500, but 20% of NPR 200 order = NPR 40 cap
        result = calculate_redemption_discount(self.TEST_EMAIL, 500, 200)
        self.assertTrue(result["ok"])
        self.assertEqual(result["discount"], 40)

    def test_redemption_below_minimum_fails(self):
        from saathimart.api.loyalty import calculate_redemption_discount
        result = calculate_redemption_discount(self.TEST_EMAIL, 10, 1000)
        # min_points_to_redeem = 50, requesting 10 → fail
        self.assertFalse(result["ok"])

    def test_tier_resolution(self):
        from saathimart.api.loyalty import earn_points, get_tier
        # Add Silver tier
        prog = frappe.get_doc("Loyalty Program", "Test Rewards")
        if not any(t.tier_name == "Silver" for t in prog.tiers):
            prog.append("tiers", {"tier_name": "Silver", "min_points": 100, "multiplier": 1.5})
            prog.save(ignore_permissions=True)

        earn_points(self.TEST_EMAIL, "TEST-ORD-006", 15000)  # 150 points
        info = get_tier(self.TEST_EMAIL, "Test Rewards")
        self.assertEqual(info["current_tier"]["tier_name"], "Silver")

    def test_order_insert_redeems_points(self):
        """
        Regression test: Order._redeem_loyalty_points() used to only be
        called from on_submit(), which nothing in the app ever calls — so
        redeemed points were discounted off the order total but never
        actually debited from the customer's balance. It now fires from
        after_insert(), which runs exactly once per order.
        """
        from saathimart.api.loyalty import earn_points, get_balance
        earn_points(self.TEST_EMAIL, "TEST-ORD-REDEEM-SETUP", 20000)  # 200 points
        balance_before = get_balance(self.TEST_EMAIL)
        self.assertEqual(balance_before, 200)

        product = _make_product("Loyalty Redeem Product", price=1000, prices=[])
        order = frappe.new_doc("Order")
        order.customer_name    = "Loyalty Test Customer"
        order.customer_email   = self.TEST_EMAIL
        order.customer_phone   = "9800000000"
        order.delivery_address = "Test Address"
        order.payment_method   = "COD"
        order.loyalty_points_redeemed = 100
        order.append("items", {"product": product.name, "product_name": product.product_name,
                                "qty": 1, "rate": 1000})
        order.insert(ignore_permissions=True)

        # validate() resolved the real discount via calculate_redemption_discount
        self.assertEqual(order.loyalty_discount, 100)

        entry = frappe.db.get_value(
            "Loyalty Point Entry",
            {"customer_email": self.TEST_EMAIL, "order": order.name, "entry_type": "Redeemed"},
            "points",
        )
        self.assertEqual(entry, 100)
        self.assertEqual(get_balance(self.TEST_EMAIL), balance_before - 100)


# ── Test: Cart ────────────────────────────────────────────────────────────────

class TestCart(unittest.TestCase):

    SESSION = "cart-test-session-xyz"

    def setUp(self):
        frappe.set_user("Guest")
        # Clean up any existing cart for this session
        name = frappe.db.get_value("Cart", {"session_id": self.SESSION}, "name")
        if name:
            frappe.delete_doc("Cart", name, ignore_permissions=True)
        frappe.db.commit()
        self.product = _make_product("Cart Test Product", price=200, stock=100)

    def test_get_or_create_cart(self):
        from saathimart.api.cart import get_cart
        cart = get_cart(self.SESSION)
        self.assertIsNotNone(cart)

    def test_add_to_cart(self):
        from saathimart.api.cart import add_to_cart, get_cart
        add_to_cart(self.SESSION, self.product.name, qty=2)
        cart = get_cart(self.SESSION)
        self.assertEqual(len(cart["items"]), 1)
        self.assertEqual(cart["items"][0]["qty"], 2)

    def test_add_same_product_increments_qty(self):
        from saathimart.api.cart import add_to_cart, get_cart
        add_to_cart(self.SESSION, self.product.name, qty=1)
        add_to_cart(self.SESSION, self.product.name, qty=3)
        cart = get_cart(self.SESSION)
        self.assertEqual(cart["items"][0]["qty"], 4)

    def test_update_cart_item(self):
        from saathimart.api.cart import add_to_cart, update_cart_item, get_cart
        add_to_cart(self.SESSION, self.product.name, qty=5)
        update_cart_item(self.SESSION, self.product.name, qty=2)
        cart = get_cart(self.SESSION)
        self.assertEqual(cart["items"][0]["qty"], 2)

    def test_remove_item_by_zero_qty(self):
        from saathimart.api.cart import add_to_cart, update_cart_item, get_cart
        add_to_cart(self.SESSION, self.product.name, qty=3)
        update_cart_item(self.SESSION, self.product.name, qty=0)
        cart = get_cart(self.SESSION)
        self.assertEqual(len(cart["items"]), 0)

    def test_clear_cart(self):
        from saathimart.api.cart import add_to_cart, clear_cart, get_cart
        add_to_cart(self.SESSION, self.product.name, qty=2)
        clear_cart(self.SESSION)
        cart = get_cart(self.SESSION)
        self.assertEqual(len(cart["items"]), 0)

    def test_subtotal_calculated(self):
        from saathimart.api.cart import add_to_cart, get_cart
        add_to_cart(self.SESSION, self.product.name, qty=3)
        cart = get_cart(self.SESSION)
        self.assertEqual(cart["subtotal"], 600)  # 3 × 200

    def test_inactive_product_rejected(self):
        from saathimart.api.cart import add_to_cart
        self.product.db_set("status", "Inactive")
        with self.assertRaises(frappe.ValidationError):
            add_to_cart(self.SESSION, self.product.name, qty=1)
        self.product.db_set("status", "Active")


# ── Test: Order / Checkout ────────────────────────────────────────────────────

class TestCheckout(unittest.TestCase):

    SESSION = "checkout-test-session-abc"

    def setUp(self):
        frappe.set_user("Administrator")
        self.zone    = _make_zone("Checkout Test Zone", charge=100, free_above=2000)
        self.product = _make_product("Checkout Product", price=500, stock=100)

        # Clean cart
        name = frappe.db.get_value("Cart", {"session_id": self.SESSION}, "name")
        if name:
            frappe.delete_doc("Cart", name, ignore_permissions=True)
        frappe.db.commit()

        # Add item to cart
        frappe.set_user("Guest")
        from saathimart.api.cart import add_to_cart
        add_to_cart(self.SESSION, self.product.name, qty=2)
        frappe.set_user("Administrator")

    def test_checkout_creates_order(self):
        from saathimart.api.orders import checkout
        result = checkout(
            session_id=self.SESSION,
            customer_name="Ram Bahadur",
            customer_phone="9801234567",
            delivery_address="Thamel, Kathmandu",
            payment_method="COD",
            delivery_zone=self.zone.name,
        )
        self.assertIn("order_id", result)
        order = frappe.get_doc("Order", result["order_id"])
        self.assertEqual(order.customer_name, "Ram Bahadur")
        self.assertEqual(len(order.items), 1)
        self.assertEqual(order.items[0].qty, 2)

    def test_checkout_totals_correct(self):
        from saathimart.api.orders import checkout
        result = checkout(
            session_id=self.SESSION,
            customer_name="Sita Devi",
            customer_phone="9807654321",
            delivery_address="Lalitpur",
            payment_method="eSewa",
            delivery_zone=self.zone.name,
        )
        order = frappe.get_doc("Order", result["order_id"])
        # 2 × 500 = 1000 + 100 delivery = 1100
        self.assertEqual(order.net_total, 1000)
        self.assertEqual(order.delivery_charge, 100)
        self.assertEqual(order.grand_total, 1100)

    def test_checkout_with_coupon(self):
        from saathimart.api.orders import checkout
        _make_coupon("CHECKOUT10", coupon_type="Percentage", pct=10, min_order=0)
        result = checkout(
            session_id=self.SESSION,
            customer_name="Hari Prasad",
            customer_phone="9812345678",
            delivery_address="Bhaktapur",
            payment_method="COD",
            delivery_zone=self.zone.name,
            coupon_code="CHECKOUT10",
        )
        order = frappe.get_doc("Order", result["order_id"])
        # 10% of 1000 = 100 discount → grand = 1000 - 100 + 100 delivery = 1000
        self.assertEqual(order.coupon_discount, 100)
        self.assertEqual(order.grand_total, 1000)

    def test_checkout_free_delivery_above_threshold(self):
        from saathimart.api.orders import checkout
        # Add more items to exceed free_delivery_above=2000
        frappe.set_user("Guest")
        from saathimart.api.cart import add_to_cart
        add_to_cart(self.SESSION, self.product.name, qty=3)  # total 5 × 500 = 2500
        frappe.set_user("Administrator")

        result = checkout(
            session_id=self.SESSION,
            customer_name="Krishna Lal",
            customer_phone="9800000001",
            delivery_address="Pokhara",
            payment_method="COD",
            delivery_zone=self.zone.name,
        )
        order = frappe.get_doc("Order", result["order_id"])
        self.assertEqual(order.delivery_charge, 0)

    def test_empty_cart_raises(self):
        from saathimart.api.orders import checkout
        from saathimart.api.cart import clear_cart
        clear_cart(self.SESSION)
        with self.assertRaises(frappe.ValidationError):
            checkout(
                session_id=self.SESSION,
                customer_name="Test",
                customer_phone="9800000000",
                delivery_address="Test",
            )

    def test_cart_marked_checked_out(self):
        from saathimart.api.orders import checkout
        result = checkout(
            session_id=self.SESSION,
            customer_name="Gita Sharma",
            customer_phone="9800000002",
            delivery_address="Kathmandu",
            payment_method="COD",
        )
        cart_status = frappe.db.get_value(
            "Cart", {"session_id": self.SESSION}, "status"
        )
        self.assertEqual(cart_status, "CheckedOut")


# ── Test: Checkout vendor routing + stock reservation ──────────────────────────
# checkout() must know which vendor an order belongs to (Order.vendor +
# per-item vendor) and must reserve that vendor's Vendor Stock atomically —
# see saathimart.api.stock.atomic_reserve.

class TestCheckoutVendorRouting(unittest.TestCase):

    SESSION = "checkout-vendor-session"

    def setUp(self):
        frappe.set_user("Administrator")
        self.vendor_a = _make_vendor("Checkout Vendor A", slug="checkout-vendor-a")
        self.vendor_b = _make_vendor("Checkout Vendor B", slug="checkout-vendor-b")
        self.product = _make_product("Checkout Vendor Product", price=100, prices=[
            {"price_type": "Site Price", "vendor": self.vendor_a.name,
             "price": 100, "min_qty": 1, "is_active": 1},
            {"price_type": "Site Price", "vendor": self.vendor_b.name,
             "price": 110, "min_qty": 1, "is_active": 1},
        ])
        _seed_vendor_stock(self.vendor_a.name, self.product.name, available=10)
        _seed_vendor_stock(self.vendor_b.name, self.product.name, available=10)

        name = frappe.db.get_value("Cart", {"session_id": self.SESSION}, "name")
        if name:
            frappe.delete_doc("Cart", name, ignore_permissions=True)
        frappe.db.commit()

    def _checkout(self, **kwargs):
        from saathimart.api.orders import checkout
        defaults = dict(
            session_id=self.SESSION, customer_name="Test Customer",
            customer_phone="9800000000", delivery_address="Kathmandu",
            payment_method="COD",
        )
        defaults.update(kwargs)
        return checkout(**defaults)

    def test_checkout_sets_order_vendor_and_copies_item_vendor(self):
        from saathimart.api.cart import add_to_cart
        frappe.set_user("Guest")
        add_to_cart(self.SESSION, self.product.name, qty=2, vendor=self.vendor_a.name)
        frappe.set_user("Administrator")

        result = self._checkout()
        self.assertEqual(result["vendor"], self.vendor_a.name)
        order = frappe.get_doc("Order", result["order_id"])
        self.assertEqual(order.vendor, self.vendor_a.name)
        self.assertEqual(order.items[0].vendor, self.vendor_a.name)

    def test_checkout_reserves_vendor_stock(self):
        from saathimart.api.cart import add_to_cart
        from saathimart.api.stock import get_vendor_stock
        frappe.set_user("Guest")
        add_to_cart(self.SESSION, self.product.name, qty=3, vendor=self.vendor_a.name)
        frappe.set_user("Administrator")

        self._checkout()
        stock = get_vendor_stock(self.vendor_a.name, self.product.name)
        self.assertEqual(stock["available_qty"], 7)
        self.assertEqual(stock["reserved_qty"], 3)
        # vendor_b's stock is untouched — reservation is per-vendor
        stock_b = get_vendor_stock(self.vendor_b.name, self.product.name)
        self.assertEqual(stock_b["available_qty"], 10)

    def test_checkout_insufficient_vendor_stock_raises(self):
        from saathimart.api.cart import add_to_cart
        from saathimart.api.stock import get_vendor_stock
        frappe.set_user("Guest")
        add_to_cart(self.SESSION, self.product.name, qty=999, vendor=self.vendor_a.name)
        frappe.set_user("Administrator")

        with self.assertRaises(frappe.ValidationError):
            self._checkout()
        # failed reservation must not have partially applied
        stock = get_vendor_stock(self.vendor_a.name, self.product.name)
        self.assertEqual(stock["available_qty"], 10)

    def test_checkout_mixed_vendor_cart_creates_separate_fulfillments(self):
        """
        Mixed-vendor carts are supported: checkout splits items into one
        Vendor Fulfillment row per vendor, each with its own subtotal, and
        reserves stock independently against each vendor's Vendor Stock row.
        """
        from saathimart.api.cart import add_to_cart
        from saathimart.api.stock import get_vendor_stock
        product_b = _make_product("Checkout Vendor Product B", price=50, prices=[
            {"price_type": "Site Price", "vendor": self.vendor_b.name,
             "price": 60, "min_qty": 1, "is_active": 1},
        ])
        _seed_vendor_stock(self.vendor_b.name, product_b.name, available=10)

        frappe.set_user("Guest")
        add_to_cart(self.SESSION, self.product.name, qty=1, vendor=self.vendor_a.name)
        add_to_cart(self.SESSION, product_b.name, qty=2, vendor=self.vendor_b.name)
        frappe.set_user("Administrator")

        result = self._checkout()
        order = frappe.get_doc("Order", result["order_id"])

        self.assertEqual(len(order.vendor_fulfillments), 2)
        by_vendor = {f.vendor: f for f in order.vendor_fulfillments}
        self.assertIn(self.vendor_a.name, by_vendor)
        self.assertIn(self.vendor_b.name, by_vendor)
        self.assertEqual(by_vendor[self.vendor_a.name].subtotal, 100)   # 1 × 100
        self.assertEqual(by_vendor[self.vendor_b.name].subtotal, 120)   # 2 × 60
        self.assertEqual(order.grand_total, 220 + (order.delivery_charge or 0))

        stock_a = get_vendor_stock(self.vendor_a.name, self.product.name)
        stock_b = get_vendor_stock(self.vendor_b.name, product_b.name)
        self.assertEqual(stock_a["reserved_qty"], 1)
        self.assertEqual(stock_b["reserved_qty"], 2)

    def test_checkout_mixed_vendor_cart_visible_to_both_vendors(self):
        """
        Regression: list_orders() used to filter SM Vendor users by the
        legacy Order.vendor field only, so on a multi-vendor order every
        vendor except the first (arbitrary) one in Order.vendor was blind
        to their own order. It must now resolve via Vendor Fulfillment rows.
        """
        from saathimart.api.cart import add_to_cart
        from saathimart.api.orders import list_orders
        product_b = _make_product("Checkout Vendor Product C", price=40, prices=[
            {"price_type": "Site Price", "vendor": self.vendor_b.name,
             "price": 45, "min_qty": 1, "is_active": 1},
        ])
        _seed_vendor_stock(self.vendor_b.name, product_b.name, available=10)

        frappe.set_user("Guest")
        add_to_cart(self.SESSION, self.product.name, qty=1, vendor=self.vendor_a.name)
        add_to_cart(self.SESSION, product_b.name, qty=1, vendor=self.vendor_b.name)
        frappe.set_user("Administrator")

        result = self._checkout()

        user_a = _make_vendor_user("checkout-vendor-a-list@test.np", self.vendor_a.name)
        user_b = _make_vendor_user("checkout-vendor-b-list@test.np", self.vendor_b.name)

        frappe.set_user(user_a.name)
        orders_for_a = list_orders()
        frappe.set_user(user_b.name)
        orders_for_b = list_orders()
        frappe.set_user("Administrator")

        self.assertIn(result["order_id"], [o["name"] for o in orders_for_a])
        self.assertIn(result["order_id"], [o["name"] for o in orders_for_b])

    def test_checkout_without_vendor_is_unaffected(self):
        """Legacy hub-fulfilled path — no vendor on any cart item."""
        from saathimart.api.cart import add_to_cart
        frappe.set_user("Guest")
        add_to_cart(self.SESSION, self.product.name, qty=1)
        frappe.set_user("Administrator")

        result = self._checkout()
        self.assertEqual(result["vendor"], "")
        stock = frappe.db.get_value(
            "Vendor Stock", f"{self.vendor_a.name}-{self.product.name}", "available_qty"
        )
        self.assertEqual(stock, 10)  # untouched


# ── Test: Order status transitions ────────────────────────────────────────────

class TestOrderStatus(unittest.TestCase):

    def _create_order(self):
        frappe.set_user("Administrator")
        product = _make_product("Status Test Product", price=300, stock=50)
        order = frappe.new_doc("Order")
        order.customer_name  = "Test Customer"
        order.customer_email = "status@test.np"
        order.customer_phone = "9800000099"
        order.delivery_address = "Test Address"
        order.payment_method = "COD"
        order.append("items", {"product": product.name, "product_name": product.product_name,
                                "qty": 1, "rate": 300})
        order.insert(ignore_permissions=True)
        return order

    def test_valid_transition_pending_to_confirmed(self):
        from saathimart.api.orders import update_order_status
        order = self._create_order()
        result = update_order_status(order.name, "Confirmed")
        self.assertEqual(result["status"], "Confirmed")

    def test_invalid_transition_raises(self):
        from saathimart.api.orders import update_order_status
        order = self._create_order()
        with self.assertRaises(frappe.ValidationError):
            update_order_status(order.name, "Delivered")  # can't skip steps

    def test_full_happy_path(self):
        from saathimart.api.orders import update_order_status
        order = self._create_order()
        for status in ["Confirmed", "Preparing", "Out for Delivery", "Delivered"]:
            result = update_order_status(order.name, status)
            self.assertEqual(result["status"], status)


# ── Test: Order status → vendor stock release/confirm ──────────────────────────

class TestOrderStatusVendorStock(unittest.TestCase):

    def setUp(self):
        frappe.set_user("Administrator")
        self.vendor = _make_vendor("Order Status Vendor", slug="order-status-vendor")
        self.product = _make_product("Order Status Vendor Product", price=200)
        _seed_vendor_stock(self.vendor.name, self.product.name, available=10)

    def _create_vendor_order(self, qty=2):
        order = frappe.new_doc("Order")
        order.customer_name    = "Test Customer"
        order.customer_phone   = "9800000000"
        order.delivery_address = "Test Address"
        order.payment_method   = "COD"
        order.vendor            = self.vendor.name
        order.append("items", {
            "product": self.product.name, "product_name": self.product.product_name,
            "qty": qty, "rate": 200, "vendor": self.vendor.name,
        })
        order.insert(ignore_permissions=True)
        # Mirror what checkout() does — reserve before the order exists.
        from saathimart.api.stock import atomic_reserve
        atomic_reserve(self.vendor.name, self.product.name, qty)
        return order

    def test_cancelled_releases_vendor_reservation(self):
        from saathimart.api.orders import update_order_status
        from saathimart.api.stock import get_vendor_stock
        order = self._create_vendor_order(qty=3)
        update_order_status(order.name, "Confirmed")
        update_order_status(order.name, "Cancelled")
        stock = get_vendor_stock(self.vendor.name, self.product.name)
        self.assertEqual(stock["available_qty"], 10)
        self.assertEqual(stock["reserved_qty"], 0)

    def test_delivered_confirms_vendor_deduction(self):
        from saathimart.api.orders import update_order_status
        from saathimart.api.stock import get_vendor_stock
        order = self._create_vendor_order(qty=4)
        for status in ["Confirmed", "Preparing", "Out for Delivery", "Delivered"]:
            update_order_status(order.name, status)
        stock = get_vendor_stock(self.vendor.name, self.product.name)
        self.assertEqual(stock["reserved_qty"], 0)
        self.assertEqual(stock["physical_qty"], 6)  # 10 - 4 delivered
        self.assertEqual(stock["available_qty"], 6)  # unchanged since reserve time


# ── Test: Multi-vendor order status sync ────────────────────────────────────
# A multi-vendor order's whole-order status is derived from its Vendor
# Fulfillment rows: it's only as advanced as the slowest active vendor, and
# only Cancelled once every vendor has cancelled their part. See
# saathimart.api.orders._recompute_order_status_from_fulfillments and the
# inbound handlers in saathimart.api.events.

class TestMultiVendorOrderStatusSync(unittest.TestCase):

    def setUp(self):
        frappe.set_user("Administrator")
        self.vendor_a = _make_vendor("Status Sync Vendor A", slug="status-sync-vendor-a")
        self.vendor_b = _make_vendor("Status Sync Vendor B", slug="status-sync-vendor-b")
        self.product_a = _make_product("Status Sync Product A", price=100)
        self.product_b = _make_product("Status Sync Product B", price=100)
        _seed_vendor_stock(self.vendor_a.name, self.product_a.name, available=10)
        _seed_vendor_stock(self.vendor_b.name, self.product_b.name, available=10)

    def _make_multi_vendor_order(self):
        from saathimart.api.stock import atomic_reserve
        order = frappe.new_doc("Order")
        order.customer_name    = "Multi Vendor Customer"
        order.customer_email   = "multivendor@test.np"
        order.customer_phone   = "9800000000"
        order.delivery_address = "Test Address"
        order.payment_method   = "COD"
        order.vendor            = self.vendor_a.name
        order.append("items", {
            "product": self.product_a.name, "product_name": self.product_a.product_name,
            "qty": 1, "rate": 100, "vendor": self.vendor_a.name,
        })
        order.append("items", {
            "product": self.product_b.name, "product_name": self.product_b.product_name,
            "qty": 1, "rate": 100, "vendor": self.vendor_b.name,
        })
        order.append("vendor_fulfillments", {
            "vendor": self.vendor_a.name, "subtotal": 100, "items_count": 1, "status": "Pending",
        })
        order.append("vendor_fulfillments", {
            "vendor": self.vendor_b.name, "subtotal": 100, "items_count": 1, "status": "Pending",
        })
        order.insert(ignore_permissions=True)
        atomic_reserve(self.vendor_a.name, self.product_a.name, 1)
        atomic_reserve(self.vendor_b.name, self.product_b.name, 1)
        return order

    def _fulfillment_statuses(self, order_name):
        return {f.vendor: f.status for f in frappe.get_doc("Order", order_name).vendor_fulfillments}

    def test_one_vendor_confirming_does_not_advance_whole_order(self):
        from saathimart.api.events import _handle_inbound
        order = self._make_multi_vendor_order()
        _handle_inbound("order.confirmed", {"order_id": order.name, "vendor_id": self.vendor_a.name})
        self.assertEqual(frappe.db.get_value("Order", order.name, "status"), "Pending")
        statuses = self._fulfillment_statuses(order.name)
        self.assertEqual(statuses[self.vendor_a.name], "Confirmed")
        self.assertEqual(statuses[self.vendor_b.name], "Pending")

    def test_order_advances_once_both_vendors_confirm(self):
        from saathimart.api.events import _handle_inbound
        order = self._make_multi_vendor_order()
        _handle_inbound("order.confirmed", {"order_id": order.name, "vendor_id": self.vendor_a.name})
        _handle_inbound("order.confirmed", {"order_id": order.name, "vendor_id": self.vendor_b.name})
        self.assertEqual(frappe.db.get_value("Order", order.name, "status"), "Confirmed")

    def test_delivery_only_deducts_reporting_vendors_stock(self):
        from saathimart.api.events import _handle_inbound
        from saathimart.api.stock import get_vendor_stock
        order = self._make_multi_vendor_order()
        _handle_inbound("order.delivered", {"order_id": order.name, "vendor_id": self.vendor_a.name})

        stock_a = get_vendor_stock(self.vendor_a.name, self.product_a.name)
        stock_b = get_vendor_stock(self.vendor_b.name, self.product_b.name)
        self.assertEqual(stock_a["reserved_qty"], 0)   # vendor A's slice confirmed
        self.assertEqual(stock_b["reserved_qty"], 1)   # untouched — vendor B hasn't delivered
        # Whole order hasn't advanced — vendor B is still the slowest (Pending)
        self.assertEqual(frappe.db.get_value("Order", order.name, "status"), "Pending")

    def test_order_becomes_delivered_once_both_vendors_deliver(self):
        from saathimart.api.events import _handle_inbound
        order = self._make_multi_vendor_order()
        _handle_inbound("order.delivered", {"order_id": order.name, "vendor_id": self.vendor_a.name})
        self.assertNotEqual(frappe.db.get_value("Order", order.name, "status"), "Delivered")
        _handle_inbound("order.delivered", {"order_id": order.name, "vendor_id": self.vendor_b.name})
        self.assertEqual(frappe.db.get_value("Order", order.name, "status"), "Delivered")

    def test_one_vendor_cancelling_does_not_cancel_whole_order(self):
        from saathimart.api.events import _handle_inbound
        from saathimart.api.stock import get_vendor_stock
        order = self._make_multi_vendor_order()
        _handle_inbound("order.cancel", {
            "order_id": order.name, "vendor_id": self.vendor_a.name, "reason": "Out of stock",
        })

        self.assertNotEqual(frappe.db.get_value("Order", order.name, "status"), "Cancelled")
        stock_a = get_vendor_stock(self.vendor_a.name, self.product_a.name)
        self.assertEqual(stock_a["available_qty"], 10)  # released
        statuses = self._fulfillment_statuses(order.name)
        self.assertEqual(statuses[self.vendor_a.name], "Cancelled")
        self.assertEqual(statuses[self.vendor_b.name], "Pending")

    def test_order_cancelled_once_all_vendors_cancel(self):
        from saathimart.api.events import _handle_inbound
        order = self._make_multi_vendor_order()
        _handle_inbound("order.cancel", {"order_id": order.name, "vendor_id": self.vendor_a.name})
        _handle_inbound("order.cancel", {"order_id": order.name, "vendor_id": self.vendor_b.name})
        self.assertEqual(frappe.db.get_value("Order", order.name, "status"), "Cancelled")

    def test_admin_status_update_cascades_to_fulfillments(self):
        from saathimart.api.orders import update_order_status
        order = self._make_multi_vendor_order()
        update_order_status(order.name, "Confirmed")
        statuses = self._fulfillment_statuses(order.name)
        self.assertEqual(statuses[self.vendor_a.name], "Confirmed")
        self.assertEqual(statuses[self.vendor_b.name], "Confirmed")


# ── Test: Vendor Stock — atomic reserve / release / confirm ────────────────────

class TestVendorStock(unittest.TestCase):

    def setUp(self):
        frappe.set_user("Administrator")
        self.vendor = _make_vendor("Stock Test Vendor", slug="stock-test-vendor")
        self.product = _make_product("Stock Test Product", price=100)
        _seed_vendor_stock(self.vendor.name, self.product.name, available=10)

    def test_atomic_reserve_moves_available_to_reserved(self):
        from saathimart.api.stock import atomic_reserve, get_vendor_stock
        atomic_reserve(self.vendor.name, self.product.name, 4)
        stock = get_vendor_stock(self.vendor.name, self.product.name)
        self.assertEqual(stock["available_qty"], 6)
        self.assertEqual(stock["reserved_qty"], 4)

    def test_atomic_reserve_insufficient_stock_raises(self):
        from saathimart.api.stock import atomic_reserve
        with self.assertRaises(frappe.ValidationError):
            atomic_reserve(self.vendor.name, self.product.name, 999)

    def test_atomic_reserve_exact_last_unit_then_next_fails(self):
        """The core oversell-prevention guarantee: once available hits 0,
        the next reservation attempt for the same row must fail cleanly."""
        from saathimart.api.stock import atomic_reserve, get_vendor_stock
        atomic_reserve(self.vendor.name, self.product.name, 10)
        stock = get_vendor_stock(self.vendor.name, self.product.name)
        self.assertEqual(stock["available_qty"], 0)
        with self.assertRaises(frappe.ValidationError):
            atomic_reserve(self.vendor.name, self.product.name, 1)

    def test_release_reservation_restores_available(self):
        from saathimart.api.stock import atomic_reserve, release_reservation, get_vendor_stock
        atomic_reserve(self.vendor.name, self.product.name, 5)
        release_reservation(self.vendor.name, self.product.name, 5)
        stock = get_vendor_stock(self.vendor.name, self.product.name)
        self.assertEqual(stock["available_qty"], 10)
        self.assertEqual(stock["reserved_qty"], 0)

    def test_confirm_deduction_reduces_reserved_and_physical_not_available(self):
        from saathimart.api.stock import atomic_reserve, confirm_deduction, get_vendor_stock
        atomic_reserve(self.vendor.name, self.product.name, 3)
        confirm_deduction(self.vendor.name, self.product.name, 3, order_id="TEST-ORD-CONF-1")
        stock = get_vendor_stock(self.vendor.name, self.product.name)
        self.assertEqual(stock["reserved_qty"], 0)
        self.assertEqual(stock["physical_qty"], 7)
        self.assertEqual(stock["available_qty"], 7)  # already dropped at reserve time

    def test_get_vendor_stock_returns_zero_for_unknown_row(self):
        from saathimart.api.stock import get_vendor_stock
        unknown_vendor = _make_vendor("No Stock Vendor", slug="no-stock-vendor")
        stock = get_vendor_stock(unknown_vendor.name, self.product.name)
        self.assertEqual(stock["available_qty"], 0)
        self.assertEqual(stock["physical_qty"], 0)

    def test_reservation_is_isolated_per_vendor(self):
        from saathimart.api.stock import atomic_reserve, get_vendor_stock
        vendor_b = _make_vendor("Stock Test Vendor B", slug="stock-test-vendor-b")
        _seed_vendor_stock(vendor_b.name, self.product.name, available=20)

        atomic_reserve(self.vendor.name, self.product.name, 10)
        stock_a = get_vendor_stock(self.vendor.name, self.product.name)
        stock_b = get_vendor_stock(vendor_b.name, self.product.name)
        self.assertEqual(stock_a["available_qty"], 0)
        self.assertEqual(stock_b["available_qty"], 20)  # untouched


# ── Test: Vendor stock events (inbound from saathimart-vendor) ─────────────────

class TestVendorStockEvents(unittest.TestCase):

    def setUp(self):
        frappe.set_user("Administrator")
        self.vendor = _make_vendor("Event Test Vendor", slug="event-test-vendor")
        self.product = _make_product("Event Test Product", price=150, prices=[])
        frappe.db.set_value("Product", self.product.name, "sku", "EVT-BARCODE-001")
        self.product.reload()
        # Reset to a known baseline — apply_vendor_stock_event is delta-based,
        # so re-running this suite without a zeroed row would accumulate.
        _seed_vendor_stock(self.vendor.name, self.product.name, available=0, reserved=0)

    def test_apply_vendor_stock_event_receipt_increases_available_and_physical(self):
        from saathimart.api.stock import apply_vendor_stock_event, get_vendor_stock
        apply_vendor_stock_event("stock.receipt", {
            "hub_product": self.product.name,
            "vendor_id": self.vendor.name,
            "qty_change": 20,
            "voucher_no": "PR-TEST-001",
        })
        stock = get_vendor_stock(self.vendor.name, self.product.name)
        self.assertEqual(stock["available_qty"], 20)
        self.assertEqual(stock["physical_qty"], 20)

    def test_apply_vendor_stock_event_resolves_by_barcode_fallback(self):
        from saathimart.api.stock import apply_vendor_stock_event, get_vendor_stock
        apply_vendor_stock_event("stock.receipt", {
            "barcode": "EVT-BARCODE-001",
            "vendor_id": self.vendor.name,
            "qty_change": 5,
        })
        stock = get_vendor_stock(self.vendor.name, self.product.name)
        self.assertEqual(stock["available_qty"], 5)

    def test_apply_vendor_stock_event_unknown_vendor_does_not_raise(self):
        from saathimart.api.stock import apply_vendor_stock_event
        apply_vendor_stock_event("stock.receipt", {
            "hub_product": self.product.name,
            "vendor_id": "not-a-real-vendor",
            "qty_change": 5,
        })  # must log, not raise

    def test_apply_vendor_stock_event_unmapped_product_does_not_raise(self):
        from saathimart.api.stock import apply_vendor_stock_event
        apply_vendor_stock_event("stock.receipt", {
            "barcode": "NO-SUCH-BARCODE",
            "vendor_id": self.vendor.name,
            "qty_change": 5,
        })  # must log, not raise

    def test_apply_vendor_stock_event_rejects_oversized_qty_change(self):
        from saathimart.api.stock import apply_vendor_stock_event, get_vendor_stock
        apply_vendor_stock_event("stock.receipt", {
            "hub_product": self.product.name,
            "vendor_id": self.vendor.name,
            "qty_change": 5000,
        })
        stock = get_vendor_stock(self.vendor.name, self.product.name)
        self.assertEqual(stock["available_qty"], 0)  # rejected, not applied

    def test_handle_inbound_routes_stock_deduct_with_vendor_id(self):
        """Regression test for the vendor↔hub payload key mismatch — the
        vendor app sends hub_product/qty_change/vendor_id, not
        product_id/qty/vendor, and stock.deduct/adjustment used to be
        silently dropped entirely."""
        from saathimart.api.events import _handle_inbound
        from saathimart.api.stock import get_vendor_stock
        _seed_vendor_stock(self.vendor.name, self.product.name, available=10)
        _handle_inbound("stock.deduct", {
            "hub_product": self.product.name,
            "vendor_id": self.vendor.name,
            "qty_change": -3,
            "voucher_no": "SI-TEST-001",
        })
        stock = get_vendor_stock(self.vendor.name, self.product.name)
        self.assertEqual(stock["available_qty"], 7)

    def test_handle_inbound_legacy_stock_receipt_without_vendor_still_works(self):
        from saathimart.api.events import _handle_inbound
        before = frappe.db.get_value("Product", self.product.name, "stock_qty")
        _handle_inbound("stock.receipt", {
            "product_id": self.product.name,
            "qty": 15,
            "voucher_no": "LEGACY-001",
        })
        after = frappe.db.get_value("Product", self.product.name, "stock_qty")
        self.assertEqual(after, before + 15)

    def test_order_delivered_event_confirms_deduction_and_sets_status(self):
        from saathimart.api.events import _handle_inbound
        from saathimart.api.stock import atomic_reserve, get_vendor_stock
        _seed_vendor_stock(self.vendor.name, self.product.name, available=10)
        atomic_reserve(self.vendor.name, self.product.name, 2)

        order = frappe.new_doc("Order")
        order.customer_name = "Test"
        order.customer_phone = "9800000000"
        order.delivery_address = "Test"
        order.payment_method = "COD"
        order.vendor = self.vendor.name
        order.status = "Out for Delivery"
        order.append("items", {"product": self.product.name,
                                "product_name": self.product.product_name,
                                "qty": 2, "rate": 150, "vendor": self.vendor.name})
        order.insert(ignore_permissions=True)

        _handle_inbound("order.delivered", {"order_id": order.name})
        self.assertEqual(frappe.db.get_value("Order", order.name, "status"), "Delivered")
        stock = get_vendor_stock(self.vendor.name, self.product.name)
        self.assertEqual(stock["reserved_qty"], 0)
        self.assertEqual(stock["physical_qty"], 8)

    def test_order_cancel_event_from_vendor_releases_reservation(self):
        from saathimart.api.events import _handle_inbound
        from saathimart.api.stock import atomic_reserve, get_vendor_stock
        _seed_vendor_stock(self.vendor.name, self.product.name, available=10)
        atomic_reserve(self.vendor.name, self.product.name, 4)

        order = frappe.new_doc("Order")
        order.customer_name = "Test"
        order.customer_phone = "9800000000"
        order.delivery_address = "Test"
        order.payment_method = "COD"
        order.vendor = self.vendor.name
        order.status = "Confirmed"
        order.append("items", {"product": self.product.name,
                                "product_name": self.product.product_name,
                                "qty": 4, "rate": 150, "vendor": self.vendor.name})
        order.insert(ignore_permissions=True)

        _handle_inbound("order.cancel", {"order_id": order.name, "reason": "Out of stock"})
        self.assertEqual(frappe.db.get_value("Order", order.name, "status"), "Cancelled")
        stock = get_vendor_stock(self.vendor.name, self.product.name)
        self.assertEqual(stock["available_qty"], 10)
        self.assertEqual(stock["reserved_qty"], 0)


# ── Test: eSewa signature ─────────────────────────────────────────────────────

class TestEsewaSignature(unittest.TestCase):

    def setUp(self):
        frappe.set_user("Administrator")
        s = frappe.get_single("Settings")
        s.esewa_secret_key    = "8gBm/:&EnhH.1/q"  # eSewa sandbox secret
        s.esewa_merchant_code = "EPAYTEST"
        s.payment_sandbox_mode = 1
        s.save(ignore_permissions=True)

    def test_valid_signature_accepted(self):
        import base64, hashlib, hmac
        from saathimart.api.payments import _verify_esewa_signature

        secret = "8gBm/:&EnhH.1/q"
        payload = {
            "total_amount": "1100",
            "transaction_uuid": "SM-ORD-2024-00001",
            "product_code": "EPAYTEST",
            "status": "COMPLETE",
            "signed_field_names": "total_amount,transaction_uuid,product_code",
        }
        msg = "total_amount=1100,transaction_uuid=SM-ORD-2024-00001,product_code=EPAYTEST"
        sig = base64.b64encode(
            hmac.new(secret.encode(), msg.encode(), hashlib.sha256).digest()
        ).decode()
        payload["signature"] = sig

        ok, err = _verify_esewa_signature(payload)
        self.assertTrue(ok)
        self.assertIsNone(err)

    def test_invalid_signature_rejected(self):
        from saathimart.api.payments import _verify_esewa_signature
        payload = {
            "total_amount": "1100",
            "transaction_uuid": "SM-ORD-2024-00001",
            "product_code": "EPAYTEST",
            "signed_field_names": "total_amount,transaction_uuid,product_code",
            "signature": "invalidsignature==",
        }
        ok, err = _verify_esewa_signature(payload)
        self.assertFalse(ok)
        self.assertIsNotNone(err)

    def test_missing_signed_fields_rejected(self):
        from saathimart.api.payments import _verify_esewa_signature
        payload = {"total_amount": "1100"}  # no signed_field_names
        ok, err = _verify_esewa_signature(payload)
        self.assertFalse(ok)


# ── Test: Stock Ledger Entry ─────────────────────────────────────────────────

class TestStockLedger(unittest.TestCase):

    def setUp(self):
        frappe.set_user("Administrator")
        self.product = _make_product("SLE Test Product", price=100, stock=50)
        # Reset stock to known value
        frappe.db.set_value("Product", self.product.name, "stock_qty", 50)
        frappe.db.delete("Stock Ledger Entry", {"product": self.product.name})
        frappe.db.commit()

    def test_make_entry_reduces_stock(self):
        from saathimart.saathimart.doctype.stock_ledger_entry.stock_ledger_entry import make_entry
        balance = make_entry(self.product.name, -10, "Order", "TEST-ORD-SLE-001")
        self.assertEqual(balance, 40)
        self.assertEqual(frappe.db.get_value("Product", self.product.name, "stock_qty"), 40)

    def test_make_entry_increases_stock(self):
        from saathimart.saathimart.doctype.stock_ledger_entry.stock_ledger_entry import make_entry
        balance = make_entry(self.product.name, 20, "Receipt", "VENDOR-PUSH-001",
                             source_site="vendor.saathimart.np")
        self.assertEqual(balance, 70)
        self.assertEqual(frappe.db.get_value("Product", self.product.name, "stock_qty"), 70)

    def test_sle_row_created(self):
        from saathimart.saathimart.doctype.stock_ledger_entry.stock_ledger_entry import make_entry
        make_entry(self.product.name, -5, "Order", "TEST-ORD-SLE-002")
        count = frappe.db.count("Stock Ledger Entry", {"product": self.product.name})
        self.assertEqual(count, 1)

    def test_cancellation_restores_stock(self):
        from saathimart.saathimart.doctype.stock_ledger_entry.stock_ledger_entry import make_entry
        make_entry(self.product.name, -10, "Order", "TEST-ORD-SLE-003")
        make_entry(self.product.name, 10, "Order Cancellation", "TEST-ORD-SLE-003")
        self.assertEqual(frappe.db.get_value("Product", self.product.name, "stock_qty"), 50)

    def test_vendor_stock_push_via_event(self):
        from saathimart.api.events import _apply_stock_receipt
        _apply_stock_receipt({
            "product_id": self.product.name,
            "qty": 30,
            "source_site": "vendor.saathimart.np",
            "voucher_no": "VENDOR-RECEIPT-001",
            "remarks": "Restock from vendor",
        })
        frappe.db.commit()
        self.assertEqual(frappe.db.get_value("Product", self.product.name, "stock_qty"), 80)
        sle = frappe.db.get_value(
            "Stock Ledger Entry",
            {"product": self.product.name, "voucher_type": "Receipt"},
            ["qty_change", "source_site"],
            as_dict=True,
        )
        self.assertEqual(sle.qty_change, 30)
        self.assertEqual(sle.source_site, "vendor.saathimart.np")

    def test_non_tracked_product_skipped(self):
        from saathimart.saathimart.doctype.stock_ledger_entry.stock_ledger_entry import make_entry
        frappe.db.set_value("Product", self.product.name, "track_inventory", 0)
        balance = make_entry(self.product.name, -10, "Order", "TEST-ORD-SLE-004")
        # stock_qty unchanged, no SLE row
        self.assertEqual(frappe.db.count("Stock Ledger Entry", {"product": self.product.name}), 0)
        frappe.db.set_value("Product", self.product.name, "track_inventory", 1)


# ── Test: Auth guards ─────────────────────────────────────────────────────────

class TestAuthGuards(unittest.TestCase):

    def test_list_orders_requires_login(self):
        from saathimart.api.orders import list_orders
        frappe.set_user("Guest")
        # Guest has no SM Admin / SM Vendor role → should get empty or raise
        # list_orders filters by role — Guest gets nothing, not an error
        result = list_orders()
        self.assertIsInstance(result, list)

    def test_update_status_requires_admin_or_vendor(self):
        from saathimart.api.orders import update_order_status
        frappe.set_user("Guest")
        with self.assertRaises(frappe.PermissionError):
            update_order_status("FAKE-ORDER", "Confirmed")

    def test_has_app_permission_admin(self):
        from saathimart.api.auth import has_app_permission
        frappe.set_user("Administrator")
        self.assertTrue(has_app_permission())


# ── Test: CMS API ──────────────────────────────────────────────────────────────

class TestCmsApi(unittest.TestCase):

    def setUp(self):
        frappe.set_user("Administrator")
        for key in ("sm_site_config", "sm_banners"):
            frappe.cache().delete_key(key)

    def test_get_site_config_defaults_copyright_year_when_unset(self):
        from saathimart.api.cms import get_site_config
        doc = frappe.get_single("Site Config")
        doc.site_title = "CMS Test Site"
        doc.copyright_year = 0
        doc.save(ignore_permissions=True)
        frappe.cache().delete_key("sm_site_config")

        data = get_site_config()
        self.assertEqual(data["copyright_year"], frappe.utils.now_datetime().year)

    def test_get_site_config_respects_explicit_copyright_year(self):
        from saathimart.api.cms import get_site_config
        doc = frappe.get_single("Site Config")
        doc.copyright_year = 2020
        doc.save(ignore_permissions=True)
        frappe.cache().delete_key("sm_site_config")

        data = get_site_config()
        self.assertEqual(data["copyright_year"], 2020)
        doc.copyright_year = 0  # reset so other tests get the live-year default
        doc.save(ignore_permissions=True)
        frappe.cache().delete_key("sm_site_config")

    def test_get_site_config_is_cached_between_calls(self):
        from saathimart.api.cms import get_site_config
        first = get_site_config()
        frappe.db.set_value("Site Config", "Site Config", "tagline", "Changed but not busted")
        second = get_site_config()
        self.assertEqual(first["tagline"], second["tagline"])

    def _make_page(self, slug, sections=None, status="Published"):
        existing = frappe.db.exists("Site Page", {"slug": slug})
        if existing:
            frappe.delete_doc("Site Page", existing, ignore_permissions=True, force=True)
        doc = frappe.new_doc("Site Page")
        doc.slug = slug
        doc.title = f"Test Page {slug}"
        doc.breadcrumb_label = "TEST PAGE"
        doc.subtitle = "A page used for testing"
        doc.status = status
        doc.sections = json.dumps(sections if sections is not None else [
            {"kind": "paragraph", "segments": [{"type": "text", "text": "Hello"}]},
        ])
        doc.insert(ignore_permissions=True)
        return doc

    def test_get_page_returns_parsed_sections(self):
        from saathimart.api.cms import get_page
        self._make_page("cms-test-page-1")
        frappe.cache().delete_key("sm_page:cms-test-page-1")

        data = get_page("cms-test-page-1")
        self.assertEqual(data["breadcrumb_label"], "TEST PAGE")
        self.assertIsInstance(data["sections"], list)
        self.assertEqual(data["sections"][0]["kind"], "paragraph")

    def test_get_page_not_found_raises(self):
        from saathimart.api.cms import get_page
        with self.assertRaises(frappe.DoesNotExistError):
            get_page("does-not-exist-anywhere")

    def test_get_page_draft_not_returned(self):
        from saathimart.api.cms import get_page
        self._make_page("cms-test-draft-page", status="Draft")
        with self.assertRaises(frappe.DoesNotExistError):
            get_page("cms-test-draft-page")

    def test_get_page_cache_busted_on_update(self):
        from saathimart.api.cms import get_page
        doc = self._make_page("cms-test-page-cache")
        frappe.cache().delete_key("sm_page:cms-test-page-cache")
        first = get_page("cms-test-page-cache")
        self.assertEqual(first["title"], "Test Page cms-test-page-cache")

        doc.reload()
        doc.title = "Renamed Test Page"
        doc.save(ignore_permissions=True)  # triggers _bust_page_cache via on_update
        second = get_page("cms-test-page-cache")
        self.assertEqual(second["title"], "Renamed Test Page")

    def _make_nav_item(self, label, location="Header", parent=None, is_active=1):
        existing = frappe.db.exists("Navigation Item", {"label": label, "menu_location": location})
        if existing:
            return frappe.get_doc("Navigation Item", existing)
        doc = frappe.new_doc("Navigation Item")
        doc.label = label
        doc.url = f"/{frappe.scrub(label)}"
        doc.menu_location = location
        doc.is_active = is_active
        if parent:
            doc.parent_item = parent
        doc.insert(ignore_permissions=True)
        return doc

    def test_get_navigation_returns_active_items_with_children(self):
        from saathimart.api.cms import get_navigation
        frappe.cache().delete_key("sm_navigation:Header")
        parent = self._make_nav_item("CMS Test Parent")
        self._make_nav_item("CMS Test Child", parent=parent.name)

        items = get_navigation("Header")
        match = next((i for i in items if i["label"] == "CMS Test Parent"), None)
        self.assertIsNotNone(match)
        self.assertEqual(len(match["children"]), 1)
        self.assertEqual(match["children"][0]["label"], "CMS Test Child")

    def test_get_navigation_excludes_inactive(self):
        from saathimart.api.cms import get_navigation
        frappe.cache().delete_key("sm_navigation:Footer")
        self._make_nav_item("CMS Test Inactive", location="Footer", is_active=0)

        items = get_navigation("Footer")
        labels = [i["label"] for i in items]
        self.assertNotIn("CMS Test Inactive", labels)

    def _make_banner(self, title, banner_type="Hero", valid_from=None, valid_to=None):
        existing = frappe.db.exists("Banner", {"title": title})
        if existing:
            frappe.delete_doc("Banner", existing, ignore_permissions=True, force=True)
        doc = frappe.new_doc("Banner")
        doc.title = title
        doc.banner_type = banner_type
        doc.heading = title
        doc.is_active = 1
        if valid_from:
            doc.valid_from = valid_from
        if valid_to:
            doc.valid_to = valid_to
        doc.insert(ignore_permissions=True)
        return doc

    def test_get_banners_filters_by_type(self):
        from saathimart.api.cms import get_banners
        self._make_banner("CMS Test Hero Banner", banner_type="Hero")
        self._make_banner("CMS Test Promo Banner", banner_type="Promo Strip")

        hero_only = get_banners(banner_type="Hero")
        titles = [b["title"] for b in hero_only]
        self.assertIn("CMS Test Hero Banner", titles)
        self.assertNotIn("CMS Test Promo Banner", titles)

    def test_get_banners_includes_stable_id(self):
        from saathimart.api.cms import get_banners
        self._make_banner("CMS Test Id Banner", banner_type="Hero")
        result = get_banners(banner_type="Hero")
        match = next(b for b in result if b["title"] == "CMS Test Id Banner")
        self.assertEqual(match["id"], "cms-test-id-banner")

    def test_get_banners_excludes_expired(self):
        from saathimart.api.cms import get_banners
        from frappe.utils import add_days
        self._make_banner("CMS Test Expired Banner", banner_type="Promo Strip",
                          valid_to=add_days(today(), -1))
        result = get_banners(banner_type="Promo Strip")
        titles = [b["title"] for b in result]
        self.assertNotIn("CMS Test Expired Banner", titles)

    def test_get_banners_excludes_not_yet_valid(self):
        from saathimart.api.cms import get_banners
        from frappe.utils import add_days
        self._make_banner("CMS Test Future Banner", banner_type="Promo Strip",
                          valid_from=add_days(today(), 5))
        result = get_banners(banner_type="Promo Strip")
        titles = [b["title"] for b in result]
        self.assertNotIn("CMS Test Future Banner", titles)


# ── Test: Vendor Commission Reconciliation report ────────────────────────────
# Aggregates by Vendor Fulfillment (not the legacy Order.vendor field), so a
# multi-vendor order correctly contributes a separate row/amount to every
# vendor that fulfilled part of it.

class TestVendorCommissionReconciliationReport(unittest.TestCase):

    def setUp(self):
        frappe.set_user("Administrator")
        # A unique suffix per test method (not per class) — _make_vendor()
        # reuses an existing Vendor by name, and these tests assert *exact*
        # SUM(...) totals over a date range, so a name shared across test
        # methods would let orders from earlier methods leak into later
        # methods' aggregates.
        suffix = frappe.generate_hash(length=6)
        self.vendor_a = _make_vendor(f"Commission Vendor A {suffix}", slug=f"commission-vendor-a-{suffix}")
        self.vendor_b = _make_vendor(f"Commission Vendor B {suffix}", slug=f"commission-vendor-b-{suffix}")
        frappe.db.set_value("Vendor", self.vendor_a.name, "commission_pct", 10)
        frappe.db.set_value("Vendor", self.vendor_b.name, "commission_pct", 20)
        self.product_a = _make_product("Commission Product A", price=1000)
        self.product_b = _make_product("Commission Product B", price=500)

    def _make_order(self, vendor, product, subtotal, payment_status):
        order = frappe.new_doc("Order")
        order.customer_name    = "Commission Test Customer"
        order.customer_phone   = "9800000000"
        order.delivery_address = "Test Address"
        order.payment_method   = "COD"
        order.payment_status   = payment_status
        order.vendor            = vendor
        order.append("items", {
            "product": product, "product_name": product, "qty": 1,
            "rate": subtotal, "vendor": vendor,
        })
        order.append("vendor_fulfillments", {
            "vendor": vendor, "subtotal": subtotal, "items_count": 1, "status": "Confirmed",
        })
        order.insert(ignore_permissions=True)
        return order

    def _run(self, **filters):
        from saathimart.saathimart.report.vendor_commission_reconciliation.vendor_commission_reconciliation import execute
        return execute(filters)

    def test_settled_and_pending_split_by_payment_status(self):
        self._make_order(self.vendor_a.name, self.product_a.name, 1000, "Paid")
        self._make_order(self.vendor_a.name, self.product_a.name, 500, "Unpaid")

        columns, data = self._run(from_date=today(), to_date=today(), vendor=self.vendor_a.name)
        row = next(r for r in data if r["vendor"] == self.vendor_a.name)
        self.assertEqual(row["gross_sales"], 1500)
        self.assertEqual(row["settled_sales"], 1000)
        self.assertEqual(row["pending_sales"], 500)
        self.assertEqual(row["commission_amount"], 100)   # 10% of settled 1000
        self.assertEqual(row["payout_due"], 900)

    def test_multi_vendor_order_splits_commission_per_vendor(self):
        """
        Regression guard: aggregating by the legacy Order.vendor field (like
        vendor_performance.py does) would attribute this whole order to
        vendor A alone. This report must split it by Vendor Fulfillment so
        vendor B's 500 and 20% commission show up under vendor B, not A.
        """
        order = frappe.new_doc("Order")
        order.customer_name    = "Split Commission Customer"
        order.customer_phone   = "9800000000"
        order.delivery_address = "Test Address"
        order.payment_method   = "COD"
        order.payment_status   = "Paid"
        order.vendor            = self.vendor_a.name
        order.append("items", {"product": self.product_a.name, "product_name": self.product_a.name,
                                "qty": 1, "rate": 1000, "vendor": self.vendor_a.name})
        order.append("items", {"product": self.product_b.name, "product_name": self.product_b.name,
                                "qty": 1, "rate": 500, "vendor": self.vendor_b.name})
        order.append("vendor_fulfillments", {
            "vendor": self.vendor_a.name, "subtotal": 1000, "items_count": 1, "status": "Confirmed",
        })
        order.append("vendor_fulfillments", {
            "vendor": self.vendor_b.name, "subtotal": 500, "items_count": 1, "status": "Confirmed",
        })
        order.insert(ignore_permissions=True)

        columns, data = self._run(from_date=today(), to_date=today())
        row_a = next(r for r in data if r["vendor"] == self.vendor_a.name)
        row_b = next(r for r in data if r["vendor"] == self.vendor_b.name)
        self.assertEqual(row_a["settled_sales"], 1000)
        self.assertEqual(row_a["commission_amount"], 100)   # 10% of 1000
        self.assertEqual(row_b["settled_sales"], 500)
        self.assertEqual(row_b["commission_amount"], 100)   # 20% of 500

    def test_cancelled_fulfillment_excluded(self):
        order = self._make_order(self.vendor_a.name, self.product_a.name, 1000, "Paid")
        frappe.db.set_value("Vendor Fulfillment", {"parent": order.name}, "status", "Cancelled")

        columns, data = self._run(from_date=today(), to_date=today(), vendor=self.vendor_a.name)
        self.assertFalse(any(r["vendor"] == self.vendor_a.name for r in data))

    def test_vendor_role_sees_only_own_commission(self):
        self._make_order(self.vendor_a.name, self.product_a.name, 1000, "Paid")
        self._make_order(self.vendor_b.name, self.product_b.name, 500, "Paid")

        user_a = _make_vendor_user("commission-vendor-a-report@test.np", self.vendor_a.name)
        frappe.set_user(user_a.name)
        try:
            columns, data = self._run(from_date=today(), to_date=today())
        finally:
            frappe.set_user("Administrator")

        vendors_seen = {r["vendor"] for r in data}
        self.assertEqual(vendors_seen, {self.vendor_a.name})


# ── Test: Vendor Payout ──────────────────────────────────────────────────────
# Vendor Payout is the ledger of what's already been paid out — it's what
# turns the commission report's per-period snapshot into an actual "how much
# is left to give the vendor right now" answer. See
# saathimart.api.payouts.get_outstanding_payout and
# saathimart.saathimart.doctype.vendor_payout.

class TestVendorPayout(unittest.TestCase):

    def setUp(self):
        frappe.set_user("Administrator")
        # See TestVendorCommissionReconciliationReport.setUp — a unique
        # suffix per test method keeps this test's orders from leaking into
        # (or being polluted by) other methods' exact SUM(...) assertions.
        suffix = frappe.generate_hash(length=6)
        self.vendor = _make_vendor(f"Payout Test Vendor {suffix}", slug=f"payout-test-vendor-{suffix}")
        frappe.db.set_value("Vendor", self.vendor.name, "commission_pct", 10)
        self.product = _make_product("Payout Test Product", price=1000)

    def _make_paid_order(self, subtotal=1000, status="Confirmed"):
        order = frappe.new_doc("Order")
        order.customer_name    = "Payout Test Customer"
        order.customer_phone   = "9800000000"
        order.delivery_address = "Test Address"
        order.payment_method   = "COD"
        order.payment_status   = "Paid"
        order.vendor            = self.vendor.name
        order.append("items", {
            "product": self.product.name, "product_name": self.product.product_name,
            "qty": 1, "rate": subtotal, "vendor": self.vendor.name,
        })
        order.append("vendor_fulfillments", {
            "vendor": self.vendor.name, "subtotal": subtotal, "items_count": 1, "status": status,
        })
        order.insert(ignore_permissions=True)
        return order

    def test_outstanding_payout_zero_with_no_paid_orders(self):
        from saathimart.api.payouts import get_outstanding_payout
        result = get_outstanding_payout(self.vendor.name)
        self.assertEqual(result["gross_sales"], 0)
        self.assertEqual(result["payout_due"], 0)

    def test_outstanding_payout_computes_commission(self):
        from saathimart.api.payouts import get_outstanding_payout
        self._make_paid_order(subtotal=1000)
        result = get_outstanding_payout(self.vendor.name)
        self.assertEqual(result["gross_sales"], 1000)
        self.assertEqual(result["commission_amount"], 100)
        self.assertEqual(result["payout_due"], 900)

    def test_create_payout_settles_outstanding_balance(self):
        from saathimart.api.payouts import get_outstanding_payout, create_vendor_payout
        self._make_paid_order(subtotal=1000)
        result = create_vendor_payout(
            self.vendor.name, add_days(today(), -1), today(), payment_reference="TXN-001",
        )
        self.assertEqual(result["payout_amount"], 900)
        self.assertEqual(result["fulfillments_count"], 1)

        after = get_outstanding_payout(self.vendor.name)
        self.assertEqual(after["gross_sales"], 0)
        self.assertEqual(after["payout_due"], 0)

    def test_create_payout_twice_does_not_double_pay(self):
        from saathimart.api.payouts import create_vendor_payout
        self._make_paid_order(subtotal=1000)
        create_vendor_payout(self.vendor.name, add_days(today(), -1), today())
        with self.assertRaises(frappe.ValidationError):
            create_vendor_payout(self.vendor.name, add_days(today(), -1), today())

    def test_deleting_payout_reopens_the_balance(self):
        from saathimart.api.payouts import get_outstanding_payout, create_vendor_payout
        self._make_paid_order(subtotal=1000)
        result = create_vendor_payout(self.vendor.name, add_days(today(), -1), today())
        frappe.delete_doc("Vendor Payout", result["payout_id"], ignore_permissions=True)

        after = get_outstanding_payout(self.vendor.name)
        self.assertEqual(after["gross_sales"], 1000)
        self.assertEqual(after["payout_due"], 900)

    def test_unpaid_order_not_counted_as_outstanding(self):
        from saathimart.api.payouts import get_outstanding_payout
        order = self._make_paid_order(subtotal=1000)
        frappe.db.set_value("Order", order.name, "payment_status", "Unpaid")
        result = get_outstanding_payout(self.vendor.name)
        self.assertEqual(result["gross_sales"], 0)

    def test_cancelled_fulfillment_not_counted_as_outstanding(self):
        from saathimart.api.payouts import get_outstanding_payout
        self._make_paid_order(subtotal=1000, status="Cancelled")
        result = get_outstanding_payout(self.vendor.name)
        self.assertEqual(result["gross_sales"], 0)

    def test_vendor_role_can_only_see_own_outstanding_payout(self):
        from saathimart.api.payouts import get_outstanding_payout
        self._make_paid_order(subtotal=1000)
        other_vendor = _make_vendor("Payout Test Vendor Other", slug="payout-test-vendor-other")
        user = _make_vendor_user("payout-vendor-user@test.np", self.vendor.name)

        frappe.set_user(user.name)
        try:
            with self.assertRaises(frappe.PermissionError):
                get_outstanding_payout(other_vendor.name)
        finally:
            frappe.set_user("Administrator")

    def test_report_shows_already_paid_out_after_payout(self):
        from saathimart.saathimart.report.vendor_commission_reconciliation.vendor_commission_reconciliation import execute
        from saathimart.api.payouts import create_vendor_payout
        self._make_paid_order(subtotal=1000)
        create_vendor_payout(self.vendor.name, add_days(today(), -1), today())

        columns, data = execute({
            "from_date": add_days(today(), -1), "to_date": today(), "vendor": self.vendor.name,
        })
        row = next(r for r in data if r["vendor"] == self.vendor.name)
        self.assertEqual(row["already_paid_out"], 900)
        self.assertEqual(row["payout_due"], 0)
