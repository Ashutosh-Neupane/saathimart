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
from unittest.mock import MagicMock, patch

import frappe
from frappe.utils import flt, today, add_days


# ── Module-level fixture isolation ──────────────────────────────────────────
# Settings is a Frappe Single — TestLoyalty, TestLocationBasedLoyalty, and
# TestEsewaSignature all overwrite the one real row for this site (loyalty
# program + eSewa credentials), and this file uses plain unittest.TestCase
# (not frappe.tests.utils.FrappeTestCase), which gives no automatic per-test
# rollback. Left unrestored, a live hub's real loyalty/payment config
# silently ends up holding test fixture values after `bench run-tests`
# finishes — the vendor side of this codebase hit the equivalent bug for
# real (Vendor Config.hub_url) and it broke live sync until manually caught.
# setUpModule/tearDownModule run exactly once around this whole file's test
# run, so whatever was really configured before the suite ran is restored
# after, without touching the individual tests that rely on mutating it
# mid-suite.
_SETTINGS_FIELDS = [
    "enable_loyalty", "loyalty_program", "esewa_merchant_code", "payment_sandbox_mode",
]
_original_settings = None
_original_esewa_secret = None


def setUpModule():
    global _original_settings, _original_esewa_secret
    frappe.set_user("Administrator")
    doc = frappe.get_single("Settings")
    _original_settings = {f: doc.get(f) for f in _SETTINGS_FIELDS}
    _original_esewa_secret = doc.get_password("esewa_secret_key", raise_exception=False)


def tearDownModule():
    if _original_settings is None:
        return
    frappe.set_user("Administrator")
    doc = frappe.get_single("Settings")
    for field, value in _original_settings.items():
        doc.set(field, value)
    if _original_esewa_secret is not None:
        doc.esewa_secret_key = _original_esewa_secret
    doc.flags.ignore_mandatory = True
    doc.save(ignore_permissions=True)
    frappe.db.commit()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_product(name, price=100, stock=50, prices=None, variant_of=None, variant_attributes=None):
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
    if variant_of:
        doc.variant_of = variant_of
    for attr in (variant_attributes or []):
        doc.append("variant_attributes", attr)
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


def _make_variant_template(name):
    """A has_variants=1 template Product — no Vendor Listing/Stock of its
    own, purely a grouping shell for variant Products (see _make_product's
    variant_of/variant_attributes params)."""
    slug = frappe.scrub(name).replace("_", "-")
    if frappe.db.exists("Product", {"slug": slug}):
        return frappe.get_doc("Product", {"slug": slug})
    doc = frappe.new_doc("Product")
    doc.product_name = name
    doc.status = "Active"
    doc.has_variants = 1
    doc.insert(ignore_permissions=True)
    return doc


def _make_zone(name, charge=80, free_above=1500, loyalty_multiplier=1,
               first_order_discount_pct=0, second_order_discount_pct=0,
               onboarding_max_discount_amount=0):
    if frappe.db.exists("Delivery Zone", name):
        return frappe.get_doc("Delivery Zone", name)
    doc = frappe.new_doc("Delivery Zone")
    doc.zone_name          = name
    doc.delivery_charge    = charge
    doc.free_delivery_above = free_above
    doc.is_active          = 1
    doc.loyalty_multiplier = loyalty_multiplier
    doc.first_order_discount_pct = first_order_discount_pct
    doc.second_order_discount_pct = second_order_discount_pct
    doc.onboarding_max_discount_amount = onboarding_max_discount_amount
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
    from saathimart.api.stock import get_or_create, _invalidate_stock_cache
    row = get_or_create(vendor, product)
    frappe.db.set_value("Vendor Stock", row.name, {
        "available_qty": available,
        "reserved_qty": reserved,
        "physical_qty": available + reserved,
    })
    # get_vendor_stock caches for 30s (see api/stock.py). Vendor/product
    # identities are reused across many tests via _make_vendor/_make_product,
    # so without this, a read moments before this reseed (another test, a
    # manual diagnostic call) can serve a stale value for up to 30s after —
    # intermittent, hard-to-reproduce test flakiness.
    _invalidate_stock_cache(vendor, product)
    return row.name


def _make_coupon(code, coupon_type="Percentage", pct=10, amount=0,
                 min_order=0, max_uses=0, valid_days=30):
    if frappe.db.exists("Coupon", code):
        # Coupon Usage rows link to the coupon and block the delete —
        # leftovers from a previous run would otherwise make this (and
        # every test that reuses the code) fail with LinkExistsError.
        for usage in frappe.get_all("Coupon Usage",
                                    filters={"coupon": code}, pluck="name"):
            frappe.delete_doc("Coupon Usage", usage, ignore_permissions=True)
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

    def test_price_update_event_is_idempotent_on_event_id(self):
        from saathimart.api.events import _apply_price_update
        from saathimart.saathimart.doctype.product.product import get_effective_price

        event_id = frappe.generate_hash(length=10)
        _apply_price_update({
            "product_id": self.product.name, "vendor": self.vendor_a.name,
            "price": 150, "event_id": event_id, "event_seq": 5,
        })
        # A retried push carrying the same event_id but a different price
        # (e.g. the vendor's HTTP client timed out waiting for a response
        # the hub actually sent) must not re-apply.
        _apply_price_update({
            "product_id": self.product.name, "vendor": self.vendor_a.name,
            "price": 999, "event_id": event_id, "event_seq": 5,
        })
        price = get_effective_price(self.product, vendor=self.vendor_a.name)
        self.assertEqual(price, 150)

    def test_price_update_event_out_of_order_seq_is_ignored(self):
        from saathimart.api.events import _apply_price_update
        from saathimart.saathimart.doctype.product.product import get_effective_price

        _apply_price_update({
            "product_id": self.product.name, "vendor": self.vendor_a.name,
            "price": 200, "event_id": frappe.generate_hash(length=10), "event_seq": 10,
        })
        # Now that receive()/bulk_receive() apply events via deferred
        # background jobs (see _process_inbound_event), an older push can
        # arrive and get processed after a newer one — event_seq=7 arriving
        # after event_seq=10 was already applied must not roll price back.
        _apply_price_update({
            "product_id": self.product.name, "vendor": self.vendor_a.name,
            "price": 50, "event_id": frappe.generate_hash(length=10), "event_seq": 7,
        })
        price = get_effective_price(self.product, vendor=self.vendor_a.name)
        self.assertEqual(price, 200)

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


# ── Test: product serialization exposes stock/backorder to the frontend ─────
# _serialize_product's payload (list_products/get_product) is what a
# frontend would use to decide whether "Add to Cart" should be enabled —
# stock_qty and track_inventory alone aren't enough to make that call
# correctly, since a backorder-allowed listing at 0 stock should still be
# purchasable.

class TestProductStockSerialization(unittest.TestCase):

    def setUp(self):
        frappe.set_user("Administrator")
        self.product = _make_product("Stock Serialization Product", price=200, prices=[])

    def _set_listing(self, allow_backorder, available_qty):
        vendor = _make_vendor(
            f"Stock Serialization Vendor {allow_backorder}-{available_qty}",
            slug=f"stock-serialization-vendor-{allow_backorder}-{available_qty}",
        )
        vl = frappe.new_doc("Vendor Listing")
        vl.vendor = vendor.name
        vl.product = self.product.name
        vl.price = 200
        vl.track_inventory = 1
        vl.allow_backorder = allow_backorder
        vl.priority = 10  # outrank _make_product's own default listing
        vl.status = "Active"
        vl.insert(ignore_permissions=True)
        _seed_vendor_stock(vendor.name, self.product.name, available=available_qty)
        return vendor

    def test_get_product_exposes_allow_backorder_true(self):
        from saathimart.api.products import get_product
        vendor = self._set_listing(allow_backorder=1, available_qty=0)
        result = get_product(self.product.slug, vendor=vendor.name)
        self.assertEqual(result["allow_backorder"], 1)
        self.assertEqual(result["stock_qty"], 0)

    def test_get_product_exposes_allow_backorder_false(self):
        from saathimart.api.products import get_product
        vendor = self._set_listing(allow_backorder=0, available_qty=5)
        result = get_product(self.product.slug, vendor=vendor.name)
        self.assertEqual(result["allow_backorder"], 0)
        self.assertEqual(result["stock_qty"], 5)


# ── Test: Product variants ───────────────────────────────────────────────────
# A variant is a full Product in its own right (own slug, Vendor Listing,
# Vendor Stock, barcode) linked back to a has_variants=1 template via
# variant_of — so cart/checkout/stock/hub-vendor sync need zero changes to
# work with variants. These tests cover the template/variant-specific
# surface: validation, browse-grid aggregation, and the variant switcher.

class TestProductVariants(unittest.TestCase):

    def setUp(self):
        frappe.set_user("Administrator")
        self.template = _make_variant_template("Variant T-Shirt")
        self.red_l = _make_product(
            "Variant T-Shirt Red L", price=500, stock=10, prices=[],
            variant_of=self.template.name,
            variant_attributes=[{"attribute": "Color", "value": "Red"},
                                {"attribute": "Size", "value": "L"}],
        )
        self.blue_m = _make_product(
            "Variant T-Shirt Blue M", price=450, stock=0, prices=[],
            variant_of=self.template.name,
            variant_attributes=[{"attribute": "Color", "value": "Blue"},
                                {"attribute": "Size", "value": "M"}],
        )

    def test_variant_of_must_point_to_a_has_variants_product(self):
        plain = _make_product("Variant Test Plain Product", price=100, prices=[])
        doc = frappe.new_doc("Product")
        doc.product_name = "Bad Variant"
        doc.status = "Active"
        doc.variant_of = plain.name
        with self.assertRaises(frappe.ValidationError):
            doc.insert(ignore_permissions=True)

    def test_cannot_be_both_template_and_variant(self):
        doc = frappe.new_doc("Product")
        doc.product_name = "Confused Product"
        doc.status = "Active"
        doc.has_variants = 1
        doc.variant_of = self.template.name
        with self.assertRaises(frappe.ValidationError):
            doc.insert(ignore_permissions=True)

    def test_valid_variant_creation_succeeds(self):
        self.assertEqual(self.red_l.variant_of, self.template.name)
        self.assertEqual(len(self.red_l.variant_attributes), 2)

    def test_list_products_shows_template_not_individual_variants(self):
        from saathimart.api.products import list_products
        result = list_products(search="Variant T-Shirt")
        names = [i["name"] for i in result["items"]]
        self.assertIn(self.template.name, names)
        self.assertNotIn(self.red_l.name, names)
        self.assertNotIn(self.blue_m.name, names)

    def test_list_products_template_price_is_cheapest_in_stock_variant(self):
        from saathimart.api.products import list_products
        result = list_products(search="Variant T-Shirt")
        item = next(i for i in result["items"] if i["name"] == self.template.name)
        # blue_m (450) is cheaper but out of stock; red_l (500) is in stock —
        # the in-stock variant wins over the merely-cheaper one.
        self.assertEqual(item["price"], 500)
        self.assertTrue(item["has_variants"])

    def test_get_product_on_template_returns_variant_list(self):
        from saathimart.api.products import get_product
        result = get_product(self.template.slug)
        self.assertTrue(result["has_variants"])
        variant_names = {v["name"] for v in result["variants"]}
        self.assertEqual(variant_names, {self.red_l.name, self.blue_m.name})
        self.assertIsNone(result["variant_of_product"])

    def test_get_product_on_variant_returns_siblings_and_attributes(self):
        from saathimart.api.products import get_product
        result = get_product(self.red_l.slug)
        self.assertEqual(result["variant_of"], self.template.name)
        self.assertEqual(
            {a["attribute"]: a["value"] for a in result["variant_attributes"]},
            {"Color": "Red", "Size": "L"},
        )
        sibling_names = {v["name"] for v in result["variants"]}
        self.assertEqual(sibling_names, {self.blue_m.name})  # excludes itself
        self.assertEqual(result["variant_of_product"]["name"], self.template.name)
        # A variant is fully purchasable on its own — real price/stock, not
        # the template's aggregate.
        self.assertEqual(result["price"], 500)
        self.assertEqual(result["stock_qty"], 10)

    def test_add_to_cart_rejects_template_directly(self):
        from saathimart.api.cart import add_to_cart
        result = add_to_cart("variant-cart-session", self.template.name, qty=1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "VALIDATION_ERROR")

    def test_add_to_cart_accepts_a_specific_variant(self):
        from saathimart.api.cart import add_to_cart, get_cart
        add_to_cart("variant-cart-session-2", self.red_l.name, qty=1)
        cart = get_cart("variant-cart-session-2")
        self.assertEqual(cart["items"][0]["product"], self.red_l.name)
        self.assertEqual(cart["items"][0]["rate"], 500)


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


# ── Test: Location-based loyalty + onboarding discount ────────────────────────
# Delivery Zone.loyalty_multiplier / first_order_discount_pct /
# second_order_discount_pct — same "rate lives on the zone, not the
# customer" pattern as everything else location-based in this codebase
# (ST_Distance_Sphere vendor sorting, delivery_charge per zone).

class TestLocationBasedLoyalty(unittest.TestCase):

    TEST_EMAIL = "zone_loyalty_test@saathimart.np"

    def setUp(self):
        frappe.set_user("Administrator")
        if not frappe.db.exists("Loyalty Program", "Test Rewards"):
            prog = frappe.new_doc("Loyalty Program")
            prog.program_name = "Test Rewards"
            prog.is_active = 1
            prog.collection_factor = 0.01
            prog.redemption_factor = 1.0
            prog.min_points_to_redeem = 50
            prog.max_redemption_per_order_pct = 20
            prog.point_expiry_days = 365
            prog.insert(ignore_permissions=True)

        s = frappe.get_single("Settings")
        s.enable_loyalty = 1
        s.loyalty_program = "Test Rewards"
        s.save(ignore_permissions=True)

        frappe.db.delete("Loyalty Point Entry", {"customer_email": self.TEST_EMAIL})
        frappe.db.commit()

        self.product = _make_product("Zone Loyalty Product", price=1000, prices=[])
        self.zone_bonus = _make_zone("Zone Loyalty Bonus", loyalty_multiplier=2)
        self.zone_plain = _make_zone("Zone Loyalty Plain", loyalty_multiplier=1)

    def _make_order(self, zone, customer_email=None):
        order = frappe.new_doc("Order")
        order.customer_name = "Zone Loyalty Customer"
        order.customer_email = customer_email or self.TEST_EMAIL
        order.customer_phone = "9800000000"
        order.delivery_address = "Test Address"
        order.delivery_zone = zone
        order.payment_method = "COD"
        order.append("items", {
            "product": self.product.name, "product_name": self.product.product_name,
            "qty": 1, "rate": 1000,
        })
        order.insert(ignore_permissions=True)
        return order

    def test_zone_multiplier_boosts_earned_points(self):
        from saathimart.api.loyalty import earn_points, get_balance
        order = self._make_order(self.zone_bonus.name)
        # 1000 × 0.01 collection_factor × 1.0 tier × 2.0 zone multiplier = 20
        earned = earn_points(self.TEST_EMAIL, order.name, 1000)
        self.assertEqual(earned, 20)
        self.assertEqual(get_balance(self.TEST_EMAIL), 20)

    def test_plain_zone_earns_standard_points(self):
        from saathimart.api.loyalty import earn_points
        order = self._make_order(self.zone_plain.name)
        earned = earn_points(self.TEST_EMAIL, order.name, 1000)
        self.assertEqual(earned, 10)

    def test_no_zone_falls_back_to_standard_rate(self):
        from saathimart.api.loyalty import earn_points
        # order_name doesn't resolve to a real Order at all — must not error,
        # must behave exactly like the un-zoned pre-existing tests.
        earned = earn_points(self.TEST_EMAIL, "NOT-A-REAL-ORDER", 1000)
        self.assertEqual(earned, 10)


class TestOnboardingDiscount(unittest.TestCase):

    TEST_EMAIL = "onboarding_test@saathimart.np"

    def setUp(self):
        frappe.set_user("Administrator")
        self.zone = _make_zone(
            "Zone Onboarding", first_order_discount_pct=20, second_order_discount_pct=10,
        )
        self.zone_capped = _make_zone(
            "Zone Onboarding Capped", first_order_discount_pct=50,
            onboarding_max_discount_amount=100,
        )
        self.zone_plain = _make_zone("Zone Onboarding Plain")
        frappe.db.delete("Order", {"customer_email": self.TEST_EMAIL})
        frappe.db.commit()

    def _base_order(self, zone, customer_email=None):
        return {
            "items": [{"product": "X", "qty": 1, "rate": 1000, "amount": 0}],
            "delivery_charge": 0,
            "discount_amount": 0,
            "coupon_code": "",
            "loyalty_points_redeemed": 0,
            "customer_email": customer_email or self.TEST_EMAIL,
            "delivery_zone": zone,
            "taxes": [],
        }

    def _place_real_order(self, zone):
        """Insert a real Order row so later sequence-count lookups see it."""
        product = _make_product("Onboarding Filler Product", price=1000, prices=[])
        order = frappe.new_doc("Order")
        order.customer_name = "Onboarding Customer"
        order.customer_email = self.TEST_EMAIL
        order.customer_phone = "9800000000"
        order.delivery_address = "Test Address"
        order.delivery_zone = zone
        order.payment_method = "COD"
        order.append("items", {
            "product": product.name, "product_name": product.product_name,
            "qty": 1, "rate": 1000,
        })
        order.insert(ignore_permissions=True)
        return order

    def test_first_order_gets_zone_discount(self):
        from saathimart.api.totals import calculate_taxes_and_totals
        order = self._base_order(self.zone.name)
        calculate_taxes_and_totals(order)
        self.assertEqual(order["onboarding_order_sequence"], 1)
        # net_total 1000 × 20% = 200
        self.assertEqual(order["onboarding_discount"], 200)

    def test_second_order_gets_lower_zone_discount(self):
        from saathimart.api.totals import calculate_taxes_and_totals
        self._place_real_order(self.zone.name)  # customer's 1st order, now in DB

        order = self._base_order(self.zone.name)
        calculate_taxes_and_totals(order)
        self.assertEqual(order["onboarding_order_sequence"], 2)
        # net_total 1000 × 10% = 100
        self.assertEqual(order["onboarding_discount"], 100)

    def test_third_order_gets_no_onboarding_discount(self):
        from saathimart.api.totals import calculate_taxes_and_totals
        self._place_real_order(self.zone.name)
        self._place_real_order(self.zone.name)

        order = self._base_order(self.zone.name)
        calculate_taxes_and_totals(order)
        self.assertEqual(order["onboarding_order_sequence"], 3)
        self.assertEqual(order["onboarding_discount"], 0)

    def test_discount_capped_by_zone_max(self):
        from saathimart.api.totals import calculate_taxes_and_totals
        order = self._base_order(self.zone_capped.name)
        calculate_taxes_and_totals(order)
        # 1000 × 50% = 500, capped at 100
        self.assertEqual(order["onboarding_discount"], 100)

    def test_zone_with_no_onboarding_rates_applies_nothing(self):
        from saathimart.api.totals import calculate_taxes_and_totals
        order = self._base_order(self.zone_plain.name)
        calculate_taxes_and_totals(order)
        self.assertEqual(order["onboarding_discount"], 0)

    def test_no_zone_applies_nothing(self):
        from saathimart.api.totals import calculate_taxes_and_totals
        order = self._base_order(zone=None)
        calculate_taxes_and_totals(order)
        self.assertEqual(order["onboarding_discount"], 0)
        self.assertEqual(order["onboarding_order_sequence"], 0)

    def test_onboarding_discount_reduces_grand_total(self):
        from saathimart.api.totals import calculate_taxes_and_totals
        order = self._base_order(self.zone.name)
        calculate_taxes_and_totals(order)
        # net_total 1000 - onboarding 200 = 800
        self.assertEqual(order["grand_total"], 800)


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

    def test_add_to_cart_out_of_stock_raises(self):
        from saathimart.api.cart import add_to_cart
        vendor = _make_vendor("Out Of Stock Vendor", slug="out-of-stock-vendor")
        vl = frappe.new_doc("Vendor Listing")
        vl.vendor = vendor.name
        vl.product = self.product.name
        vl.price = 200
        vl.track_inventory = 1
        vl.allow_backorder = 0
        vl.status = "Active"
        vl.insert(ignore_permissions=True)
        _seed_vendor_stock(vendor.name, self.product.name, available=0)

        # add_to_cart is @handle_api_errors-wrapped: frappe.throw() inside it
        # is caught and turned into {"ok": False, ...} rather than
        # propagating, so a direct Python call never raises — see
        # api/responses.py:handle_api_errors.
        result = add_to_cart(self.SESSION, self.product.name, qty=1, vendor=vendor.name)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "VALIDATION_ERROR")

    def test_add_to_cart_more_than_available_raises(self):
        from saathimart.api.cart import add_to_cart
        vendor = _make_vendor("Limited Stock Vendor", slug="limited-stock-vendor")
        vl = frappe.new_doc("Vendor Listing")
        vl.vendor = vendor.name
        vl.product = self.product.name
        vl.price = 200
        vl.track_inventory = 1
        vl.allow_backorder = 0
        vl.status = "Active"
        vl.insert(ignore_permissions=True)
        _seed_vendor_stock(vendor.name, self.product.name, available=3)

        result = add_to_cart(self.SESSION, self.product.name, qty=4, vendor=vendor.name)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "VALIDATION_ERROR")

    def test_add_to_cart_allows_backorder_when_flagged(self):
        from saathimart.api.cart import add_to_cart, get_cart
        vendor = _make_vendor("Backorder Vendor", slug="backorder-vendor")
        vl = frappe.new_doc("Vendor Listing")
        vl.vendor = vendor.name
        vl.product = self.product.name
        vl.price = 200
        vl.track_inventory = 1
        vl.allow_backorder = 1
        vl.status = "Active"
        vl.insert(ignore_permissions=True)
        _seed_vendor_stock(vendor.name, self.product.name, available=0)

        add_to_cart(self.SESSION, self.product.name, qty=2, vendor=vendor.name)
        cart = get_cart(self.SESSION)
        self.assertEqual(cart["items"][0]["qty"], 2)

    def test_add_to_cart_ignores_stock_when_not_tracked(self):
        from saathimart.api.cart import add_to_cart, get_cart
        vendor = _make_vendor("Untracked Stock Vendor", slug="untracked-stock-vendor")
        vl = frappe.new_doc("Vendor Listing")
        vl.vendor = vendor.name
        vl.product = self.product.name
        vl.price = 200
        vl.track_inventory = 0
        vl.allow_backorder = 0
        vl.status = "Active"
        vl.insert(ignore_permissions=True)
        _seed_vendor_stock(vendor.name, self.product.name, available=0)

        add_to_cart(self.SESSION, self.product.name, qty=2, vendor=vendor.name)
        cart = get_cart(self.SESSION)
        self.assertEqual(cart["items"][0]["qty"], 2)

    def test_add_to_cart_second_add_blocked_once_combined_qty_exceeds_stock(self):
        """Availability is checked against the combined qty (already-in-cart
        + this add), not just the new qty in isolation — otherwise two
        3-unit adds against 5 available stock would both succeed."""
        from saathimart.api.cart import add_to_cart
        vendor = _make_vendor("Combined Qty Vendor", slug="combined-qty-vendor")
        vl = frappe.new_doc("Vendor Listing")
        vl.vendor = vendor.name
        vl.product = self.product.name
        vl.price = 200
        vl.track_inventory = 1
        vl.allow_backorder = 0
        vl.status = "Active"
        vl.insert(ignore_permissions=True)
        _seed_vendor_stock(vendor.name, self.product.name, available=5)

        add_to_cart(self.SESSION, self.product.name, qty=3, vendor=vendor.name)
        result = add_to_cart(self.SESSION, self.product.name, qty=3, vendor=vendor.name)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "VALIDATION_ERROR")

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

    def test_update_cart_item_without_vendor_matches_the_only_line(self):
        """update_cart_item() doesn't require vendor when it's unambiguous —
        add_to_cart() auto-resolves a vendor internally even when the caller
        doesn't pass one, so a caller that only ever added one line for this
        product shouldn't need to already know which vendor got picked."""
        from saathimart.api.cart import add_to_cart, update_cart_item, get_cart
        add_to_cart(self.SESSION, self.product.name, qty=1)
        cart = get_cart(self.SESSION)
        auto_vendor = cart["items"][0]["vendor"]
        self.assertTrue(auto_vendor)  # some vendor was auto-selected

        update_cart_item(self.SESSION, self.product.name, qty=4)
        cart = get_cart(self.SESSION)
        self.assertEqual(cart["items"][0]["qty"], 4)
        self.assertEqual(cart["items"][0]["vendor"], auto_vendor)

    def test_update_cart_item_ambiguous_across_vendors_requires_vendor(self):
        """The same product sitting in the cart from two different vendors
        (test_same_product_different_vendor_are_separate_cart_lines) is a
        real ambiguity update_cart_item() can't silently guess through —
        it must ask for the vendor rather than picking one arbitrarily."""
        from saathimart.api.cart import add_to_cart, update_cart_item, get_cart
        vendor_a = _make_vendor("Cart Update Vendor A", slug="cart-update-vendor-a")
        vendor_b = _make_vendor("Cart Update Vendor B", slug="cart-update-vendor-b")
        for v in (vendor_a, vendor_b):
            vl = frappe.new_doc("Vendor Listing")
            vl.vendor = v.name
            vl.product = self.product.name
            vl.price = 200
            vl.track_inventory = 1
            vl.status = "Active"
            vl.insert(ignore_permissions=True)
            _seed_vendor_stock(v.name, self.product.name, available=20)

        add_to_cart(self.SESSION, self.product.name, qty=1, vendor=vendor_a.name)
        add_to_cart(self.SESSION, self.product.name, qty=1, vendor=vendor_b.name)
        self.assertEqual(len(get_cart(self.SESSION)["items"]), 2)

        result = update_cart_item(self.SESSION, self.product.name, qty=5)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "VALIDATION_ERROR")

        update_cart_item(self.SESSION, self.product.name, qty=5, vendor=vendor_a.name)
        cart = get_cart(self.SESSION)
        by_vendor = {i["vendor"]: i["qty"] for i in cart["items"]}
        self.assertEqual(by_vendor[vendor_a.name], 5)
        self.assertEqual(by_vendor[vendor_b.name], 1)

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
        result = add_to_cart(self.SESSION, self.product.name, qty=1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "VALIDATION_ERROR")
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
        result = checkout(
            session_id=self.SESSION,
            customer_name="Test",
            customer_phone="9800000000",
            delivery_address="Test",
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "VALIDATION_ERROR")

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

        # checkout() must run as the same user that filled the cart —
        # find_active_cart() (see cart.py) deliberately prefers a signed-in
        # user's own cart over session_id, so switching to Administrator
        # first can make it find some *other* leftover Administrator-owned
        # cart from a completely different test instead of this Guest cart.
        result = self._checkout()
        frappe.set_user("Administrator")
        self.assertEqual(result["vendor"], self.vendor_a.name)
        order = frappe.get_doc("Order", result["order_id"])
        self.assertEqual(order.vendor, self.vendor_a.name)
        self.assertEqual(order.items[0].vendor, self.vendor_a.name)

    def test_checkout_reserves_vendor_stock(self):
        from saathimart.api.cart import add_to_cart
        from saathimart.api.stock import get_vendor_stock
        frappe.set_user("Guest")
        add_to_cart(self.SESSION, self.product.name, qty=3, vendor=self.vendor_a.name)
        self._checkout()
        frappe.set_user("Administrator")
        stock = get_vendor_stock(self.vendor_a.name, self.product.name)
        self.assertEqual(stock["available_qty"], 7)
        self.assertEqual(stock["reserved_qty"], 3)
        # vendor_b's stock is untouched — reservation is per-vendor
        stock_b = get_vendor_stock(self.vendor_b.name, self.product.name)
        self.assertEqual(stock_b["available_qty"], 10)

    def test_add_to_cart_insufficient_vendor_stock_raises(self):
        """
        add_to_cart() now rejects a qty the vendor can't fulfill up front
        (see api/cart.py's availability guard) instead of silently letting
        it sit in the cart until checkout's atomic_reserve_batch fails —
        the whole point being a customer finds out immediately, not at
        payment time.
        """
        from saathimart.api.cart import add_to_cart
        from saathimart.api.stock import get_vendor_stock
        frappe.set_user("Guest")
        result = add_to_cart(self.SESSION, self.product.name, qty=999, vendor=self.vendor_a.name)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "VALIDATION_ERROR")
        frappe.set_user("Administrator")
        # rejected add must not have touched stock at all
        stock = get_vendor_stock(self.vendor_a.name, self.product.name)
        self.assertEqual(stock["available_qty"], 10)

    def test_checkout_insufficient_vendor_stock_raises(self):
        """
        checkout()'s atomic_reserve_batch remains the authoritative,
        race-safe guard even though add_to_cart() now also checks up front:
        stock can still drop between add-to-cart and checkout (another
        customer buying the last units in between). Simulate that by
        adding a qty that WAS available at add-to-cart time, then draining
        the vendor's stock before checking out.
        """
        from saathimart.api.cart import add_to_cart
        from saathimart.api.stock import get_vendor_stock
        frappe.set_user("Guest")
        add_to_cart(self.SESSION, self.product.name, qty=5, vendor=self.vendor_a.name)
        frappe.set_user("Administrator")

        # Someone else buys out the rest of vendor_a's stock in the meantime.
        # Row name needs the "-default" warehouse suffix every real Vendor
        # Stock row has (see stock.py:_row_name) — a bare f"{vendor}-{product}"
        # here targets a name that was never created, silently no-opping.
        from saathimart.api.stock import _row_name
        frappe.db.set_value("Vendor Stock", _row_name(self.vendor_a.name, self.product.name),
                             "available_qty", 0)

        # Back to Guest before checkout: find_active_cart() (cart.py)
        # prefers a signed-in user's own cart over session_id, so calling
        # this as Administrator risks finding some other leftover
        # Administrator-owned cart from a different test instead of this one.
        frappe.set_user("Guest")
        result = self._checkout()
        frappe.set_user("Administrator")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "VALIDATION_ERROR")
        # failed reservation must not have partially applied
        stock = get_vendor_stock(self.vendor_a.name, self.product.name)
        self.assertEqual(stock["available_qty"], 0)

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
        result = self._checkout()
        frappe.set_user("Administrator")
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
        result = self._checkout()
        frappe.set_user("Administrator")

        user_a = _make_vendor_user("checkout-vendor-a-list@test.np", self.vendor_a.name)
        user_b = _make_vendor_user("checkout-vendor-b-list@test.np", self.vendor_b.name)

        frappe.set_user(user_a.name)
        orders_for_a = list_orders()
        frappe.set_user(user_b.name)
        orders_for_b = list_orders()
        frappe.set_user("Administrator")

        self.assertIn(result["order_id"], [o["name"] for o in orders_for_a])
        self.assertIn(result["order_id"], [o["name"] for o in orders_for_b])

    def test_checkout_without_vendor_auto_selects_and_reserves_stock(self):
        """
        There is no untracked "hub inventory" — every product is fulfilled by
        some vendor. When add_to_cart() isn't given an explicit vendor it
        auto-resolves one via select_best_vendor() (see
        test_no_vendor_available_raises for the case where that can't find
        one at all); checkout must reserve stock against whichever vendor
        actually got picked, not silently skip stock tracking.
        """
        from saathimart.api.cart import add_to_cart
        frappe.set_user("Guest")
        add_to_cart(self.SESSION, self.product.name, qty=1)
        result = self._checkout()
        frappe.set_user("Administrator")
        self.assertTrue(result["vendor"])  # some vendor was auto-selected
        order = frappe.get_doc("Order", result["order_id"])
        self.assertEqual(order.items[0].vendor, result["vendor"])

        from saathimart.api.stock import _row_name
        reserved = frappe.db.get_value(
            "Vendor Stock", _row_name(result['vendor'], self.product.name), "reserved_qty"
        )
        self.assertEqual(reserved, 1)

    def test_no_vendor_available_raises(self):
        """A product with zero Vendor Listings can't be added to a cart at
        all — select_best_vendor() finds nothing and add_to_cart() must
        reject it rather than silently adding an untracked, vendor-less
        line (there's no pooled hub inventory to fall back to)."""
        from saathimart.api.cart import add_to_cart

        # add_to_cart is @handle_api_errors-wrapped (returns {"ok": False,
        # ...} instead of raising) — this used to assertRaises and fail
        # before ever reaching the cleanup below, leaking this product and
        # breaking every subsequent run on a duplicate-key insert. try/finally
        # now guarantees cleanup regardless of how the assertion goes.
        if frappe.db.exists("Product", {"product_name": "Orphan No Vendor Product"}):
            frappe.delete_doc(
                "Product",
                frappe.db.get_value("Product", {"product_name": "Orphan No Vendor Product"}, "name"),
                force=True,
            )
        orphan = frappe.new_doc("Product")
        orphan.product_name = "Orphan No Vendor Product"
        orphan.status = "Active"
        orphan.insert(ignore_permissions=True)

        try:
            frappe.set_user("Guest")
            result = add_to_cart(self.SESSION + "-orphan", orphan.name, qty=1)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error_code"], "VALIDATION_ERROR")
        finally:
            frappe.set_user("Administrator")
            frappe.delete_doc("Product", orphan.name, ignore_permissions=True, force=True)


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
        result = update_order_status(order.name, "Delivered")  # can't skip steps
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "VALIDATION_ERROR")

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


# ── Test: bulk_receive ────────────────────────────────────────────────────────

class TestBulkReceive(unittest.TestCase):

    def setUp(self):
        frappe.set_user("Administrator")
        self.vendor = _make_vendor("Bulk Receive Vendor", slug="bulk-receive-vendor")
        self.product_a = _make_product("Bulk Receive Product A", price=100, prices=[])
        self.product_b = _make_product("Bulk Receive Product B", price=200, prices=[])

    def test_bulk_receive_applies_every_event_in_the_batch(self):
        from saathimart.api.events import bulk_receive, _process_inbound_event
        result = bulk_receive(events=[
            {"event": "price.update", "payload": {
                "product_id": self.product_a.name, "vendor": self.vendor.name, "price": 111,
            }},
            {"event": "price.update", "payload": {
                "product_id": self.product_b.name, "vendor": self.vendor.name, "price": 222,
            }},
        ])
        self.assertTrue(all(r["ok"] for r in result["results"]))
        # bulk_receive only queues events (Webhook Event, status=Queued) and
        # fast-acks — application happens in the deferred job. Run it
        # directly here rather than relying on the real background worker.
        for r in result["results"]:
            _process_inbound_event(r["webhook_event"])
        self.assertEqual(
            frappe.db.get_value("Vendor Listing", {"product": self.product_a.name, "vendor": self.vendor.name}, "price"),
            111,
        )
        self.assertEqual(
            frappe.db.get_value("Vendor Listing", {"product": self.product_b.name, "vendor": self.vendor.name}, "price"),
            222,
        )

    def test_bulk_receive_creates_webhook_event_audit_rows(self):
        from saathimart.api.events import bulk_receive
        event_id = frappe.generate_hash(length=10)
        bulk_receive(events=[
            {"event": "price.update", "payload": {
                "product_id": self.product_a.name, "vendor": self.vendor.name,
                "price": 333, "event_id": event_id,
            }},
        ])
        self.assertTrue(frappe.db.exists("Webhook Event", {"event_id": event_id}))

    def test_bulk_receive_is_idempotent_on_event_id(self):
        from saathimart.api.events import bulk_receive, _process_inbound_event
        event_id = frappe.generate_hash(length=10)
        payload = {
            "product_id": self.product_a.name, "vendor": self.vendor.name,
            "price": 444, "event_id": event_id,
        }
        first = bulk_receive(events=[{"event": "price.update", "payload": payload}])
        _process_inbound_event(first["results"][0]["webhook_event"])
        # A second bulk call carrying the same event_id (e.g. a vendor
        # retrying an entire batch after a timeout) must not double-apply.
        result = bulk_receive(events=[{"event": "price.update", "payload": {**payload, "price": 999}}])
        self.assertEqual(result["results"][0].get("message"), "already_processed")
        self.assertEqual(
            frappe.db.get_value("Vendor Listing", {"product": self.product_a.name, "vendor": self.vendor.name}, "price"),
            444,  # not 999 — the retried duplicate was skipped, not re-applied
        )
        self.assertEqual(frappe.db.count("Webhook Event", {"event_id": event_id}), 1)

    def test_bulk_receive_continues_after_one_event_fails(self):
        from saathimart.api.events import bulk_receive, _process_inbound_event
        result = bulk_receive(events=[
            {"event": "price.update", "payload": {"product_id": "NOT-A-REAL-PRODUCT-ID"}},
            {"event": "price.update", "payload": {
                "product_id": self.product_a.name, "vendor": self.vendor.name, "price": 555,
            }},
        ])
        # Each queued event is processed as its own independent deferred
        # job, so the malformed first event (which _apply_price_update's
        # own missing-field guard logs and returns from without raising)
        # can't abort the second one — confirm it still applied.
        for r in result["results"]:
            _process_inbound_event(r["webhook_event"])
        self.assertEqual(
            frappe.db.get_value("Vendor Listing", {"product": self.product_a.name, "vendor": self.vendor.name}, "price"),
            555,
        )


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
        result = update_order_status("FAKE-ORDER", "Confirmed")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "FORBIDDEN")
        frappe.set_user("Administrator")

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
        result = get_page("does-not-exist-anywhere")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "NOT_FOUND")

    def test_get_page_draft_not_returned(self):
        from saathimart.api.cms import get_page
        self._make_page("cms-test-draft-page", status="Draft")
        result = get_page("cms-test-draft-page")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "NOT_FOUND")

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
        result = create_vendor_payout(self.vendor.name, add_days(today(), -1), today())
        self.assertFalse(result.get("ok", True))

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
            result = get_outstanding_payout(other_vendor.name)
            # handle_api_errors catches PermissionError and returns JSON
            self.assertFalse(result.get("ok", True))
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


# ── Test: variant resolver + picker metadata (options/swatches) ─────────────

class TestVariantResolver(unittest.TestCase):

    def setUp(self):
        frappe.set_user("Administrator")
        self.template = _make_variant_template("Resolver Tee")
        self.red_l = _make_product(
            "Resolver Tee Red L", price=500, stock=10, prices=[],
            variant_of=self.template.name,
            variant_attributes=[{"attribute": "Color", "value": "Red"},
                                {"attribute": "Size", "value": "L"}],
        )
        self.blue_m = _make_product(
            "Resolver Tee Blue M", price=450, stock=0, prices=[],
            variant_of=self.template.name,
            variant_attributes=[{"attribute": "Color", "value": "Blue"},
                                {"attribute": "Size", "value": "M"}],
        )

    def test_exact_match_resolves(self):
        from saathimart.api.products import get_variant
        result = get_variant(self.template.slug,
                             json.dumps({"Color": "Red", "Size": "L"}))
        self.assertEqual(result["name"], self.red_l.name)
        self.assertEqual(result["price"], 500)

    def test_attribute_names_case_insensitive(self):
        from saathimart.api.products import get_variant
        result = get_variant(self.template.slug,
                             json.dumps({"color": "Red"}))
        self.assertEqual(result["name"], self.red_l.name)

    def test_partial_selection_is_progressive(self):
        """One attribute alone → deterministic first match carrying it."""
        from saathimart.api.products import get_variant
        result = get_variant(self.template.slug, json.dumps({"Color": "Blue"}))
        self.assertEqual(result["name"], self.blue_m.name)

    def test_sibling_slug_accepted(self):
        """Landing on a variant URL and switching options must work without
        knowing the template's slug."""
        from saathimart.api.products import get_variant
        result = get_variant(self.red_l.slug, json.dumps({"Color": "Blue"}))
        self.assertEqual(result["name"], self.blue_m.name)

    def test_no_match_raises_does_not_exist(self):
        from saathimart.api.products import get_variant
        result = get_variant(self.template.slug, json.dumps({"Color": "Green"}))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "NOT_FOUND")

    def test_non_variant_slug_rejected(self):
        from saathimart.api.products import get_variant
        plain = _make_product("Resolver Plain Product", price=100, prices=[])
        result = get_variant(plain.slug, json.dumps({"Color": "Red"}))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "VALIDATION_ERROR")

    def test_invalid_attributes_json_rejected(self):
        from saathimart.api.products import get_variant
        result = get_variant(self.template.slug, "{not json")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "VALIDATION_ERROR")

    def test_template_card_exposes_options_and_count(self):
        from saathimart.api.products import list_products
        result = list_products(search="Resolver Tee")
        card = next(i for i in result["items"] if i["name"] == self.template.name)
        self.assertEqual(card["variant_count"], 2)
        options = {o["attribute"]: o["values"] for o in card.get("options", [])}
        self.assertIn("Color", options)
        color_values = {v["value"] for v in options["Color"]}
        self.assertEqual(color_values, {"Red", "Blue"})

    def test_swatch_pulls_first_thumbnail_per_value(self):
        # Give Red/L a thumbnail — it becomes the Color:Red swatch.
        frappe.db.set_value("Product", self.red_l.name, "thumbnail", "/files/red.jpg")
        try:
            from saathimart.api.products import _get_variant_options_map
            meta = _get_variant_options_map([self.template.name])
            options = {o["attribute"]: o["values"] for o in meta[self.template.name]["options"]}
            by_value = {v["value"]: v["swatch"] for v in options["Color"]}
            self.assertEqual(by_value["Red"], "/files/red.jpg")
            # No variant carries a thumbnail for its Blue-ness → no swatch.
            self.assertIsNone(by_value["Blue"])
        finally:
            frappe.db.set_value("Product", self.red_l.name, "thumbnail", "")


# ── Test: admin template/variant management API ──────────────────────────────

class TestAdminVariantManagement(unittest.TestCase):

    # Fixed literal names this class's tests create templates/variants
    # under. None of the individual tests had a delete-if-exists guard
    # (unlike _make_product elsewhere in this file), so a test that failed
    # after creating one — or simply after the doctype/field-name bugs
    # these were originally written against got fixed and the create calls
    # started succeeding — left it behind to collide with the next run's
    # duplicate-key insert. Centralized here instead of duplicating the
    # same guard in all four tests.
    _FIXTURE_TEMPLATE_NAMES = [
        "Media Cap", "Delete Sock", "Idempotent Bottle", "Admin Mgmt Mug",
    ]

    def _cleanup_fixture_templates(self):
        names = frappe.get_all(
            "Product",
            filters={"product_name": ["in", self._FIXTURE_TEMPLATE_NAMES]},
            pluck="name",
        )
        variants = frappe.get_all(
            "Product", filters={"variant_of": ["in", names or ["__none__"]]}, pluck="name"
        ) if names else []
        for n in variants + names:
            frappe.db.sql("DELETE FROM `tabVendor Listing` WHERE product = %s", n)
            frappe.db.sql("DELETE FROM `tabVendor Stock` WHERE product = %s", n)
            frappe.delete_doc("Product", n, force=True, ignore_permissions=True, ignore_missing=True)

    def setUp(self):
        frappe.set_user("Administrator")
        self._cleanup_fixture_templates()

    def tearDown(self):
        frappe.set_user("Administrator")
        self._cleanup_fixture_templates()

    def test_requires_sm_admin_role(self):
        from saathimart.api.admin_products import create_template
        frappe.set_user("Guest")
        try:
            result = create_template("Admin Guard Tee",
                            json.dumps([{"attribute": "Size", "values": ["S"]}]))
            self.assertFalse(result["ok"])
            self.assertEqual(result["error_code"], "FORBIDDEN")
        finally:
            frappe.set_user("Administrator")

    def test_create_template_and_variants_happy_path(self):
        from saathimart.api.admin_products import create_template, create_variants
        tpl = create_template(
            "Admin Mgmt Mug",
            json.dumps([{"attribute": "Color", "values": ["Black", "White"]}]),
        )
        self.assertTrue(tpl["has_variants"])

        result = create_variants(
            tpl["slug"],
            json.dumps([{"Color": "Black"}, {"Color": "White"}]),
            price=250,
        )
        self.assertEqual(len(result["created"]), 2)
        self.assertEqual(len(result["skipped"]), 0)

        created_names = [c["name"] for c in result["created"]]
        for name in created_names:
            doc = frappe.get_doc("Product", name)
            self.assertEqual(doc.variant_of, tpl["name"])
            attrs = {a.attribute: a.value for a in doc.variant_attributes}
            self.assertIn(attrs.get("Color"), ("Black", "White"))

    def test_create_variants_is_idempotent(self):
        from saathimart.api.admin_products import create_template, create_variants
        tpl = create_template(
            "Idempotent Bottle",
            json.dumps([{"attribute": "Size", "values": ["500ml"]}]),
        )
        first = create_variants(tpl["slug"], json.dumps([{"Size": "500ml"}]), price=100)
        self.assertEqual(len(first["created"]), 1)
        second = create_variants(tpl["slug"], json.dumps([{"Size": "500ml"}]), price=100)
        self.assertEqual(len(second["created"]), 0)
        self.assertEqual(len(second["skipped"]), 1)

    def test_create_template_rejects_empty_option_groups(self):
        from saathimart.api.admin_products import create_template
        result = create_template("Empty Options Product", json.dumps([{"attribute": "Size", "values": []}]))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "VALIDATION_ERROR")

    def test_update_variant_media_sets_swatch(self):
        from saathimart.api.admin_products import (
            create_template, create_variants, update_variant_media,
        )
        tpl = create_template(
            "Media Cap",
            json.dumps([{"attribute": "Color", "values": ["Navy"]}]),
        )
        result = create_variants(tpl["slug"], json.dumps([{"Color": "Navy"}]), price=300)
        vslug = result["created"][0]["slug"]

        updated = update_variant_media(vslug, thumbnail="/files/navy-cap.jpg")
        self.assertEqual(updated["thumbnail"], "/files/navy-cap.jpg")

    def test_delete_variant_soft_removes(self):
        from saathimart.api.admin_products import (
            create_template, create_variants, delete_variant,
        )
        tpl = create_template(
            "Delete Sock",
            json.dumps([{"attribute": "Size", "values": ["One Size"]}]),
        )
        result = create_variants(tpl["slug"], json.dumps([{"Size": "One Size"}]))
        vslug = result["created"][0]["slug"]
        out = delete_variant(vslug)
        self.assertEqual(out["status"], "Inactive")


# ── Inbound vendor-push authentication (HMAC signatures) ─────────────────────

class TestVerifyHubSecret(unittest.TestCase):
    """
    verify_hub_secret / verify_hub_timestamp (api/utils.py): valid HMAC
    signatures pass; tampered bodies, wrong secrets and stale timestamps
    are rejected. Covers the global-secret path, the per-vendor override,
    and dual-secret acceptance during rotation.
    """

    GLOBAL = "unit-test-global-secret-0123456789abcdef"
    VENDOR = "unit-test-vendor-secret-9876543210fedcba"
    BODY = b'{"event": "stock.low", "payload": {}}'

    def setUp(self):
        import time as _time

        frappe.set_user("Administrator")
        from frappe.utils.password import set_encrypted_password

        self._orig_global = (
            frappe.get_single("Settings").get_password("webhook_secret", raise_exception=False)
            or ""
        )
        set_encrypted_password("Settings", "Settings", self.GLOBAL, "webhook_secret")

        self.vendor = _make_vendor("HMAC Auth Test Vendor")
        set_encrypted_password("Vendor", self.vendor.name, self.VENDOR, "webhook_secret")
        frappe.db.commit()
        self.ts = str(int(_time.time()))

    def tearDown(self):
        from frappe.utils.password import set_encrypted_password

        set_encrypted_password(
            "Settings", "Settings", self._orig_global or None, "webhook_secret"
        )
        # Clear rotation fields so later tests don't inherit stale secrets.
        # Password fields live in __Auth — db.set_value to None doesn't
        # remove the encrypted row, so use db.delete instead.
        frappe.db.delete("__Auth", {
            "doctype": "Vendor",
            "name": self.vendor.name,
            "fieldname": "webhook_secret_old",
        })
        frappe.db.commit()

    def _headers(self, vendor_id=None, ts=None):
        headers = {"X-SM-Timestamp": ts or self.ts}
        if vendor_id:
            headers["X-Vendor-ID"] = vendor_id
        return headers

    def _verify(self, headers, body=None, expect_error=False):
        from saathimart.api.utils import verify_hub_secret

        fake = MagicMock()
        fake.headers = headers
        fake.get_data.return_value = self.BODY if body is None else body
        with patch("frappe.request", fake), \
             patch("saathimart.api.utils.log_auth_failure"):
            if expect_error:
                with self.assertRaises(frappe.AuthenticationError):
                    verify_hub_secret("test")
            else:
                verify_hub_secret("test")

    def _sig(self, secret, body=None, ts=None):
        from saathimart.api.utils import compute_hmac_signature

        return compute_hmac_signature(secret, ts or self.ts, self.BODY if body is None else body)

    # ── happy paths ──

    def test_valid_signature_with_global_secret_accepted(self):
        h = self._headers()
        h["X-SM-Signature"] = self._sig(self.GLOBAL)
        self._verify(h)

    def test_valid_signature_with_vendor_secret_accepted(self):
        h = self._headers(vendor_id=self.vendor.name)
        h["X-SM-Signature"] = self._sig(self.VENDOR)
        self._verify(h)

    def test_missing_signature_rejected(self):
        # No X-SM-Signature header → must reject (legacy bare-secret removed)
        self._verify(self._headers(), expect_error=True)

    # ── attack scenarios ──

    def test_tampered_body_rejected(self):
        h = self._headers()
        h["X-SM-Signature"] = self._sig(self.GLOBAL, body=self.BODY)
        tampered = self.BODY.replace(b"{}", b'{"qty": 999999}')
        self._verify(h, body=tampered, expect_error=True)

    def test_wrong_secret_rejected(self):
        h = self._headers(vendor_id=self.vendor.name)
        # Signed with the GLOBAL secret but claims the vendor identity —
        # must fail because that vendor's own secret overrides.
        h["X-SM-Signature"] = self._sig(self.GLOBAL)
        self._verify(h, expect_error=True)

    def test_garbage_signature_rejected(self):
        h = self._headers()
        h["X-SM-Signature"] = "not-a-real-signature"
        self._verify(h, expect_error=True)

    def test_stale_timestamp_rejected(self):
        import time as _time

        from saathimart.api.utils import verify_hub_timestamp

        old_ts = str(int(_time.time()) - 600)
        fake = MagicMock()
        fake.headers = {"X-SM-Timestamp": old_ts}
        with patch("frappe.request", fake):
            with self.assertRaises(frappe.AuthenticationError):
                verify_hub_timestamp(max_age_seconds=300)

    def test_fresh_timestamp_accepted(self):
        from saathimart.api.utils import verify_hub_timestamp

        fake = MagicMock()
        fake.headers = {"X-SM-Timestamp": self.ts}
        with patch("frappe.request", fake):
            verify_hub_timestamp(max_age_seconds=300)

    # ── rotation window ──

    def test_old_vendor_secret_accepted_during_rotation(self):
        from frappe.utils.password import set_encrypted_password

        set_encrypted_password("Vendor", self.vendor.name, self.GLOBAL, "webhook_secret_old")
        # NEW primary...
        h = self._headers(vendor_id=self.vendor.name)
        h["X-SM-Signature"] = self._sig(self.VENDOR)
        self._verify(h)
        # ...and OLD both verify; an unknown third secret does not.
        h2 = self._headers(vendor_id=self.vendor.name)
        h2["X-SM-Signature"] = self._sig(self.GLOBAL)
        self._verify(h2)
        h3 = self._headers(vendor_id=self.vendor.name)
        h3["X-SM-Signature"] = self._sig("third-unknown-secret-00000000000000")
        self._verify(h3, expect_error=True)

# ── Test: ERPNext Sync ───────────────────────────────────────────────────────
# Covers erpnext_sync.py: run_daily_sync, sync_single_order, _resolve_item_code,
# _sync_order_to_sales_order, _sync_order_to_invoice

class TestERPNextSync(unittest.TestCase):
    """Test ERPNext sync without actually calling the remote ERPNext site."""

    TEST_EMAIL = "erpnext_sync_test@saathimart.np"

    def setUp(self):
        frappe.set_user("Administrator")

        # Create test product with item_code mapping
        if frappe.db.exists("Product", "ERPNext Test Product"):
            existing = frappe.get_doc("Product", "ERPNext Test Product")
            frappe.db.sql("DELETE FROM `tabVendor Listing` WHERE product = %s", existing.name)
            frappe.db.sql("DELETE FROM `tabVendor Stock` WHERE product = %s", existing.name)
            frappe.db.delete("Product", existing.name)

        self.product = frappe.new_doc("Product")
        self.product.product_name = "ERPNext Test Product"
        self.product.status = "Active"
        self.product.item_code = "ERP-TEST-001"  # This item_code exists in ERPNext mock
        self.product.insert(ignore_permissions=True)

        # Create vendor with warehouse
        self.vendor = _make_vendor("ERPNext Sync Vendor", slug="erpnext-sync-vendor")
        frappe.db.set_value("Vendor", self.vendor.name, "default_warehouse", "Test Warehouse - SM")

        # Create vendor listing
        self.vl = frappe.new_doc("Vendor Listing")
        self.vl.vendor = self.vendor.name
        self.vl.product = self.product.name
        self.vl.price = 1000
        self.vl.status = "Active"
        self.vl.insert(ignore_permissions=True)

        # Seed stock
        from saathimart.api.stock import get_or_create
        row = get_or_create(self.vendor.name, self.product.name)
        frappe.db.set_value("Vendor Stock", row.name, {
            "available_qty": 10,
            "reserved_qty": 0,
            "physical_qty": 10,
        })

        # Create order
        self.order = frappe.new_doc("Order")
        self.order.customer_name = "ERPNext Test Customer"
        self.order.customer_email = self.TEST_EMAIL
        self.order.customer_phone = "9800000000"
        self.order.delivery_address = "Test Address, Kathmandu"
        self.order.delivery_zone = "Kathmandu"
        self.order.payment_method = "eSewa"
        self.order.payment_status = "Paid"
        self.order.append("items", {
            "product": self.product.name,
            "product_name": self.product.product_name,
            "qty": 2,
            "rate": 1000,
            "vendor": self.vendor.name,
        })
        self.order.append("vendor_fulfillments", {
            "vendor": self.vendor.name,
            "subtotal": 2000,
            "items_count": 1,
            "status": "Pending",
            "warehouse": "Test Warehouse - SM",
        })
        self.order.insert(ignore_permissions=True)

    def test_resolve_item_code_direct_mapping(self):
        """Test _resolve_item_code finds product.item_code directly."""
        from saathimart.api.erpnext_sync import _resolve_item_code

        # Mock ERPNext config (won't actually call ERPNext because item_code is set)
        config = {"site_url": "http://erpnext.localhost", "api_key": "test", "api_secret": "test"}

        result = _resolve_item_code(config, "ERPNext Test Product", self.product)
        self.assertEqual(result, "ERP-TEST-001")

    def test_resolve_item_code_barcode_fallback(self):
        """Test _resolve_item_code falls back to barcode lookup when item_code is empty."""
        # Temporarily clear item_code
        self.product.item_code = ""
        self.product.sku = "SKU-ERP-TEST-001"
        self.product.save(ignore_permissions=True)

        from saathimart.api.erpnext_sync import _resolve_item_code

        config = {"site_url": "http://erpnext.localhost", "api_key": "test", "api_secret": "test"}

        # This will try to query ERPNext but we mock it below
        with patch("saathimart.api.erpnext_sync._erp_get") as mock_erp_get:
            mock_erp_get.return_value = [{"parent": "ERP-ITEM-FROM-BARCODE"}]
            result = _resolve_item_code(config, "ERPNext Test Product", self.product)
            self.assertEqual(result, "ERP-ITEM-FROM-BARCODE")

    def test_resolve_item_code_name_fallback(self):
        """Test _resolve_item_code falls back to item_name match."""
        # Clear both item_code and sku
        self.product.item_code = ""
        self.product.sku = ""
        self.product.save(ignore_permissions=True)

        from saathimart.api.erpnext_sync import _resolve_item_code

        config = {"site_url": "http://erpnext.localhost", "api_key": "test", "api_secret": "test"}

        with patch("saathimart.api.erpnext_sync._erp_get") as mock_erp_get:
            mock_erp_get.return_value = [{"name": "ERP-ITEM-FROM-NAME"}]
            result = _resolve_item_code(config, "ERPNext Test Product", self.product)
            self.assertEqual(result, "ERP-ITEM-FROM-NAME")

    def test_resolve_item_code_no_match_returns_none(self):
        """Test _resolve_item_code returns None when nothing matches."""
        self.product.item_code = ""
        self.product.sku = "NON-EXISTENT-BARCODE"
        self.product.save(ignore_permissions=True)

        from saathimart.api.erpnext_sync import _resolve_item_code

        config = {"site_url": "http://erpnext.localhost", "api_key": "test", "api_secret": "test"}

        with patch("saathimart.api.erpnext_sync._erp_get") as mock_erp_get:
            mock_erp_get.return_value = []
            result = _resolve_item_code(config, "ERPNext Test Product", self.product)
            self.assertIsNone(result)

    def test_run_daily_sync_skips_when_not_configured(self):
        """Test run_daily_sync returns early when ERPNext is not configured."""
        from saathimart.api.erpnext_sync import run_daily_sync

        # Disable ERPNext sync
        s = frappe.get_single("Settings")
        s.erpnext_sync_enabled = 0
        s.save(ignore_permissions=True)

        result = run_daily_sync()
        # Should return None or empty dict since nothing was synced
        self.assertIn(result, [None, {}])

    def test_run_daily_sync_skips_when_disabled(self):
        """Test run_daily_sync returns early when ERPNext is disabled in Settings."""
        from saathimart.api.erpnext_sync import run_daily_sync

        # Enable flag but leave other fields empty
        s = frappe.get_single("Settings")
        s.erpnext_sync_enabled = 1
        s.erpnext_site_url = ""
        s.save(ignore_permissions=True)

        result = run_daily_sync()
        self.assertIn(result, [None, {}])

    def test_run_daily_sync_processes_paid_orders(self):
        """Test run_daily_sync processes paid orders with correct filters."""
        from saathimart.api.erpnext_sync import run_daily_sync

        # Mock ERPNext config to skip actual API calls but still process
        s = frappe.get_single("Settings")
        s.erpnext_sync_enabled = 1
        s.erpnext_site_url = "http://erpnext.localhost"
        s.erpnext_api_key = "test"
        s.erpnext_api_secret = "test"
        s.erpnext_company = "Test Company"
        s.erpnext_default_warehouse = "Test Warehouse - SM"
        s.save(ignore_permissions=True)

        # Order is already Paid with no sync status
        self.assertEqual(self.order.payment_status, "Paid")
        self.assertEqual(self.order.erpnext_sync_status, "")

        result = run_daily_sync()
        # Should process at least one order
        self.assertTrue(result.get("total", 0) >= 1)


# ── Test: Loyalty Birthday Rewards ────────────────────────────────────────────

class TestLoyaltyBirthdayRewards(unittest.TestCase):
    """Test check_birthday_rewards() cron function."""

    TEST_EMAIL = "birthday_test@saathimart.np"

    def setUp(self):
        frappe.set_user("Administrator")

        # Create user with birthday today
        if frappe.db.exists("User", self.TEST_EMAIL):
            frappe.delete_doc("User", self.TEST_EMAIL, ignore_permissions=True)

        from frappe.utils import today, add_days
        from datetime import datetime

        today_date = today()
        month_day = today_date.strftime("%m-%d")

        self.user = frappe.new_doc("User")
        self.user.email = self.TEST_EMAIL
        self.user.first_name = "Birthday"
        self.user.last_name = "Test"
        # Set birthday to today
        self.user.birthday = today_date
        self.user.insert(ignore_permissions=True)

        # Enable loyalty program
        if not frappe.db.exists("Loyalty Program", "Birthday Test Program"):
            prog = frappe.new_doc("Loyalty Program")
            prog.program_name = "Birthday Test Program"
            prog.is_active = 1
            prog.collection_factor = 0.01
            prog.redemption_factor = 1.0
            prog.min_points_to_redeem = 50
            prog.max_redemption_per_order_pct = 20
            prog.point_expiry_days = 365
            prog.insert(ignore_permissions=True)

        s = frappe.get_single("Settings")
        s.enable_loyalty = 1
        s.loyalty_program = "Birthday Test Program"
        s.save(ignore_permissions=True)

        # Clear any existing birthday entries for this year
        from frappe.utils import today
        marker = f"birthday:{today().year}"
        frappe.db.sql("""
            DELETE FROM `tabLoyalty Point Entry`
            WHERE customer_email = %s AND source = 'birthday' AND remarks = %s
        """, (self.TEST_EMAIL, marker))

    def tearDown(self):
        frappe.set_user("Administrator")
        frappe.db.delete("User", self.TEST_EMAIL)

    def test_check_birthday_rewards_awards_points(self):
        """Test check_birthday_rewards awards 50 points to birthday customers."""
        from saathimart.api.loyalty import check_birthday_rewards, get_balance

        # Initially no birthday points
        balance_before = get_balance(self.TEST_EMAIL)
        self.assertEqual(balance_before, 0)

        # Run the cron
        check_birthday_rewards()

        # Should have 50 points now
        balance_after = get_balance(self.TEST_EMAIL)
        self.assertEqual(balance_after, 50)

    def test_check_birthday_rewards_is_idempotent(self):
        """Test check_birthday_rewards doesn't double-award within same year."""
        from saathimart.api.loyalty import check_birthday_rewards, get_balance

        # Run first time
        check_birthday_rewards()
        balance_after_first = get_balance(self.TEST_EMAIL)
        self.assertEqual(balance_after_first, 50)

        # Run second time same day
        check_birthday_rewards()
        balance_after_second = get_balance(self.TEST_EMAIL)
        # Should still be 50, not 100
        self.assertEqual(balance_after_second, 50)

    def test_check_birthday_rewards_skips_no_birthday(self):
        """Test check_birthday_rewards doesn't award points to non-birthday customers."""
        from saathimart.api.loyalty import check_birthday_rewards, get_balance

        # Create user without birthday
        if frappe.db.exists("User", "no_birthday_test@saathimart.np"):
            frappe.delete_doc("User", "no_birthday_test@saathimart.np", ignore_permissions=True)

        user_no_birthday = frappe.new_doc("User")
        user_no_birthday.email = "no_birthday_test@saathimart.np"
        user_no_birthday.first_name = "No"
        user_no_birthday.last_name = "Birthday"
        user_no_birthday.insert(ignore_permissions=True)

        # No birthday point entries should exist
        result = frappe.db.sql("""
            SELECT COUNT(*) FROM `tabLoyalty Point Entry`
            WHERE customer_email = %s AND source = 'birthday'
        """, "no_birthday_test@saathimart.np")

        # Run the cron
        check_birthday_rewards()

        # No new birthday entries should be created
        result_after = frappe.db.sql("""
            SELECT COUNT(*) FROM `tabLoyalty Point Entry`
            WHERE customer_email = %s AND source = 'birthday'
        """, "no_birthday_test@saathimart.np")

        self.assertEqual(result[0][0], result_after[0][0])

    def test_check_birthday_rewards_skips_no_loyalty(self):
        """Test check_birthday_rewards does nothing when loyalty is disabled."""
        from saathimart.api.loyalty import check_birthday_rewards

        # Disable loyalty
        s = frappe.get_single("Settings")
        s.enable_loyalty = 0
        s.save(ignore_permissions=True)

        # Should not error, just silently do nothing
        result = check_birthday_rewards()
        self.assertIsNone(result)


# ── Test: Webhook Event Delivery ─────────────────────────────────────────────

class TestWebhookEventDelivery(unittest.TestCase):
    """Test publisher.py webhook event queuing and delivery."""

    def setUp(self):
        frappe.set_user("Administrator")

        self.vendor = _make_vendor("Webhook Test Vendor", slug="webhook-test-vendor")
        self.product = _make_product("Webhook Test Product", price=1000)

    def test_on_order_created_queues_webhook_event(self):
        """Test on_order_created creates Webhook Event for vendor notification."""
        from saathimart.events.publisher import on_order_created

        # Create order
        order = frappe.new_doc("Order")
        order.customer_name = "Webhook Test Customer"
        order.customer_phone = "9800000000"
        order.delivery_address = "Test Address"
        order.payment_method = "COD"
        order.vendor = self.vendor.name
        order.append("items", {
            "product": self.product.name,
            "product_name": self.product.product_name,
            "qty": 1,
            "rate": 1000,
            "vendor": self.vendor.name,
        })
        order.append("vendor_fulfillments", {
            "vendor": self.vendor.name,
            "subtotal": 1000,
            "items_count": 1,
            "status": "Pending",
        })
        order.insert(ignore_permissions=True)

        # Clear any existing webhook events
        frappe.db.delete("Webhook Event", {"target_vendor": self.vendor.name})

        # Trigger the event
        on_order_created(order, method="after_insert")

        # Should have queued a Webhook Event
        events = frappe.get_all("Webhook Event", {
            "target_vendor": self.vendor.name,
            "event_type": "order.new"
        }, pluck="name")

        self.assertTrue(len(events) >= 1)

    def test_on_product_created_queues_webhook_event(self):
        """Test on_product_created creates Webhook Event for barcode matching vendors."""
        from saathimart.events.publisher import on_product_created

        # Product must have SKU for notification to be queued
        self.product.sku = "SKU-WEBHOOK-001"
        self.product.save(ignore_permissions=True)

        # Clear any existing webhook events
        frappe.db.delete("Webhook Event", {})

        # Trigger the event
        on_product_created(self.product, method="after_insert")

        # Should have queued a background broadcast job (Webhook Event)
        events = frappe.get_all("Webhook Event", {
            "event_type": "product.new"
        }, pluck="name")

        self.assertTrue(len(events) >= 1)

    def test_drain_event_queue_processes_queued_events(self):
        """Test drain_event_queue picks up and processes queued events."""
        from saathimart.events.publisher import drain_event_queue, _enqueue

        # Enqueue an event
        _enqueue("stock.update", {"product": self.product.name, "qty": 100},
                 target_vendor=self.vendor.name)

        # Should have queued event
        queued = frappe.get_all("Webhook Event", {
            "target_vendor": self.vendor.name,
            "status": "Queued"
        }, pluck="name")
        self.assertTrue(len(queued) >= 1)

        # drain_event_queue enqueues delivery jobs, doesn't deliver synchronously
        # The actual delivery happens in background workers
        drain_event_queue()

        # Event should still exist (just scheduled for delivery)
        still_exists = frappe.db.exists("Webhook Event", queued[0])
        self.assertTrue(still_exists)


# ── Test: Checkout Flow ──────────────────────────────────────────────────────

class TestCheckoutFlow(unittest.TestCase):
    """Test end-to-end cart → checkout → order flow."""

    def setUp(self):
        frappe.set_user("Guest")

        self.zone = _make_zone("Checkout Test Zone", charge=80, free_above=1500)
        self.product = _make_product("Checkout Test Product", price=500, stock=100)
        self.vendor = _make_vendor("Checkout Test Vendor", slug="checkout-test-vendor")

        # Create vendor listing
        self.vl = frappe.new_doc("Vendor Listing")
        self.vl.vendor = self.vendor.name
        self.vl.product = self.product.name
        self.vl.price = 500
        self.vl.status = "Active"
        self.vl.insert(ignore_permissions=True)

        # Seed stock
        from saathimart.api.stock import get_or_create
        row = get_or_create(self.vendor.name, self.product.name)
        frappe.db.set_value("Vendor Stock", row.name, {
            "available_qty": 100,
            "reserved_qty": 0,
            "physical_qty": 100,
        })

    def test_checkout_creates_order(self):
        """Test checkout() converts cart to order."""
        from saathimart.api.cart import _get_or_create_cart, add_to_cart
        from saathimart.api.orders import checkout

        # Create cart and add item
        session_id = "checkout-test-session"
        cart = _get_or_create_cart(session_id)
        add_to_cart(session_id, self.product.name, qty=2, vendor=self.vendor.name)

        # Verify cart has item
        cart = add_to_cart(session_id, self.product.name, qty=2, vendor=self.vendor.name)
        self.assertEqual(len(cart.get("items", [])), 1)

        # Checkout
        result = checkout(
            session_id=session_id,
            customer_name="Checkout Test Customer",
            customer_phone="9800000000",
            delivery_address="Test Address, Kathmandu",
            payment_method="COD",
            delivery_zone=self.zone.name,
        )

        self.assertTrue(result.get("ok", False) or "order_id" in result)
        self.assertIn("order_id", result)

    def test_checkout_validates_cart_not_empty(self):
        """Test checkout() rejects empty cart."""
        from saathimart.api.cart import _get_or_create_cart
        from saathimart.api.orders import checkout

        session_id = "empty-cart-session"

        # Create empty cart
        cart = _get_or_create_cart(session_id)
        self.assertEqual(len(cart.items), 0)

        # Checkout should fail
        result = checkout(
            session_id=session_id,
            customer_name="Empty Cart Customer",
            customer_phone="9800000000",
            delivery_address="Test Address",
            payment_method="COD",
            delivery_zone=self.zone.name,
        )

        self.assertFalse(result.get("order_id"))
        self.assertIn("error", result)

    def test_checkout_validates_cart_has_items(self):
        """Test checkout() rejects cart with zero qty items."""
        from saathimart.api.cart import _get_or_create_cart, add_to_cart
        from saathimart.api.orders import checkout

        session_id = "zero-qty-session"

        # Add item with qty 0 (should fail at add_to_cart or validation)
        result = add_to_cart(session_id, self.product.name, qty=0, vendor=self.vendor.name)
        self.assertFalse(result.get("ok", True))

    def test_checkout_resolves_delivery_zone(self):
        """Test checkout() correctly calculates delivery charge based on zone."""
        from saathimart.api.cart import _get_or_create_cart, add_to_cart
        from saathimart.api.orders import checkout

        # Product total: 500 × 2 = 1000, below free_delivery_above (1500)
        # Should have delivery charge
        session_id = "zone-test-session"
        cart = _get_or_create_cart(session_id)
        add_to_cart(session_id, self.product.name, qty=2, vendor=self.vendor.name)

        result = checkout(
            session_id=session_id,
            customer_name="Zone Test Customer",
            customer_phone="9800000000",
            delivery_address="Test Address",
            payment_method="COD",
            delivery_zone=self.zone.name,
        )

        # Should have delivery charge since subtotal < free_above
        self.assertGreater(result.get("delivery_charge", 0), 0)

    def test_checkout_free_delivery_threshold(self):
        """Test checkout() gives free delivery when subtotal >= free_above."""
        from saathimart.api.cart import _get_or_create_cart, add_to_cart
        from saathimart.api.orders import checkout

        # Product total: 500 × 4 = 2000, above free_delivery_above (1500)
        # Should have delivery charge = 0
        session_id = "free-delivery-session"
        cart = _get_or_create_cart(session_id)
        add_to_cart(session_id, self.product.name, qty=4, vendor=self.vendor.name)

        result = checkout(
            session_id=session_id,
            customer_name="Free Delivery Customer",
            customer_phone="9800000000",
            delivery_address="Test Address",
            payment_method="COD",
            delivery_zone=self.zone.name,
        )

        self.assertEqual(result.get("delivery_charge", -1), 0)

    def test_checkout_reduces_stock(self):
        """Test checkout() reserves and deducts vendor stock."""
        from saathimart.api.cart import _get_or_create_cart, add_to_cart
        from saathimart.api.orders import checkout
        from saathimart.api.stock import get_vendor_stock

        # Record initial stock
        before = get_vendor_stock(self.vendor.name, self.product.name)

        session_id = "stock-test-session"
        add_to_cart(session_id, self.product.name, qty=3, vendor=self.vendor.name)

        result = checkout(
            session_id=session_id,
            customer_name="Stock Test Customer",
            customer_phone="9800000000",
            delivery_address="Test Address",
            payment_method="COD",
            delivery_zone=self.zone.name,
        )

        # After checkout, stock should be reduced
        after = get_vendor_stock(self.vendor.name, self.product.name)
        self.assertEqual(after["reserved_qty"], 3)

    def test_checkout_multivendor_splits_fulfillments(self):
        """Test checkout() creates separate vendor_fulfillments for multi-vendor carts."""
        from saathimart.api.cart import _get_or_create_cart, add_to_cart
        from saathimart.api.orders import checkout

        # Create second vendor
        vendor_b = _make_vendor("Checkout Vendor B", slug="checkout-vendor-b")
        product_b = _make_product("Checkout Product B", price=300, stock=50)

        # Create vendor listing for vendor_b
        vl_b = frappe.new_doc("Vendor Listing")
        vl_b.vendor = vendor_b.name
        vl_b.product = product_b.name
        vl_b.price = 300
        vl_b.status = "Active"
        vl_b.insert(ignore_permissions=True)

        # Seed stock for vendor_b
        from saathimart.api.stock import get_or_create
        row_b = get_or_create(vendor_b.name, product_b.name)
        frappe.db.set_value("Vendor Stock", row_b.name, {
            "available_qty": 50,
            "reserved_qty": 0,
            "physical_qty": 50,
        })

        session_id = "multivendor-session"
        add_to_cart(session_id, self.product.name, qty=1, vendor=self.vendor.name)
        add_to_cart(session_id, product_b.name, qty=2, vendor=vendor_b.name)

        result = checkout(
            session_id=session_id,
            customer_name="Multi Vendor Customer",
            customer_phone="9800000000",
            delivery_address="Test Address",
            payment_method="COD",
            delivery_zone=self.zone.name,
        )

        # Should have two vendor fulfillments
        fulfillments = result.get("vendor_fulfillments", [])
        self.assertEqual(len(fulfillments), 2)

        vendor_ids = [f["vendor"] for f in fulfillments]
        self.assertIn(self.vendor.name, vendor_ids)
        self.assertIn(vendor_b.name, vendor_ids)


# ── Test: Loyalty Redemption Validation ──────────────────────────────────────

class TestLoyaltyRedemptionValidation(unittest.TestCase):
    """Test loyalty point redemption validation logic."""

    TEST_EMAIL = "loyalty_validation_test@saathimart.np"

    def setUp(self):
        frappe.set_user("Administrator")

        # Create loyalty program
        if not frappe.db.exists("Loyalty Program", "Validation Test Program"):
            prog = frappe.new_doc("Loyalty Program")
            prog.program_name = "Validation Test Program"
            prog.is_active = 1
            prog.collection_factor = 0.01
            prog.redemption_factor = 1.0
            prog.min_points_to_redeem = 100
            prog.max_redemption_per_order_pct = 20
            prog.point_expiry_days = 365
            prog.insert(ignore_permissions=True)

        s = frappe.get_single("Settings")
        s.enable_loyalty = 1
        s.loyalty_program = "Validation Test Program"
        s.save(ignore_permissions=True)

        # Clear existing entries
        frappe.db.delete("Loyalty Point Entry", {"customer_email": self.TEST_EMAIL})

    def test_redemption_below_minimum_fails(self):
        """Test redeem_points() rejects redemption below min_points_to_redeem."""
        from saathimart.api.loyalty import redeem_points

        # Try to redeem 50 points when min is 100
        with self.assertRaises(Exception):
            redeem_points(self.TEST_EMAIL, "TEST-ORD-VALIDATION", 50, 50)

    def test_redemption_insufficient_balance_fails(self):
        """Test redeem_points() rejects redemption when balance is insufficient."""
        from saathimart.api.loyalty import earn_points, redeem_points

        # Earn only 50 points
        earn_points(self.TEST_EMAIL, "TEST-ORD-LOW-BALANCE", 5000)

        # Try to redeem 100 points (more than balance)
        with self.assertRaises(Exception):
            redeem_points(self.TEST_EMAIL, "TEST-ORD-VALIDATION", 100, 100)

    def test_redemption_negative_points_fails(self):
        """Test redeem_points() rejects negative point redemption."""
        from saathimart.api.loyalty import redeem_points

        with self.assertRaises(Exception):
            redeem_points(self.TEST_EMAIL, "TEST-ORD-VALIDATION", -10, -10)

    def test_redemption_positive_points_succeeds(self):
        """Test redeem_points() accepts valid redemption."""
        from saathimart.api.loyalty import earn_points, get_balance, redeem_points

        # Earn enough points
        earn_points(self.TEST_EMAIL, "TEST-ORD-VALIDATION-SETUP", 20000)  # 200 points
        balance_before = get_balance(self.TEST_EMAIL)
        self.assertEqual(balance_before, 200)

        # Redeem 100 points (valid: above min, within balance)
        redeem_points(self.TEST_EMAIL, "TEST-ORD-VALIDATION", 100, 100)

        # Balance should be reduced
        balance_after = get_balance(self.TEST_EMAIL)
        self.assertEqual(balance_after, 100)

    def test_redemption_capped_by_max_pct(self):
        """Test calculate_redemption_discount() caps redemption by max_redemption_per_order_pct."""
        from saathimart.api.loyalty import earn_points, calculate_redemption_discount

        # Earn 1000 points
        earn_points(self.TEST_EMAIL, "TEST-ORD-VALIDATION-CAP", 100000)

        # Try to redeem 500 points = NPR 500, but max is 20% of order
        # Order subtotal = 200, max discount = 40
        result = calculate_redemption_discount(self.TEST_EMAIL, 500, 200)
        self.assertTrue(result["ok"])
        self.assertEqual(result["discount"], 40)  # Capped at 20% of 200


if __name__ == "__main__":
    import unittest
    unittest.main()