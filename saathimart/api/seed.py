"""
Demo seed script — creates sample data for local development and testing.

Creates:
    - Active Vendor rows for the real site vendors (vendor1-4.localhost)
    - 6 categories, ~200 mart products (shampoos, dairy, produce, staples...)
      each with per-vendor Vendor Listing (price) + Vendor Stock (truth)
    - Storefront content: zones, slots, payment modes, coupons, loyalty,
      membership, hero slides, trust badges, offers, popular locations
    - A few demo orders (optionally; real orders should go through checkout)

Usage:
    bench --site saathimart.localhost execute saathimart.api.seed.seed_all
    bench --site saathimart.localhost execute saathimart.api.seed.clear_all
"""
import random

import frappe
from frappe.utils import add_days, now_datetime, today

# The real vendor sites of this stack. clear_all() keeps these rows —
# they hold the HMAC secrets paired with each vendor site's Vendor Config;
# deleting them would break event delivery until re-registration.
# main.localhost is NOT a Vendor row: it's the platform-ledger target,
# addressed directly by SaathiMart Settings.platform_ledger_vendor.
DEFAULT_SITE_VENDORS = [
    "vendor1.localhost",
    "vendor2.localhost",
    "vendor3.localhost",
]

_meta_cache = {}


def _has_field(doctype, fieldname):
    if doctype not in _meta_cache:
        _meta_cache[doctype] = {f.fieldname for f in frappe.get_meta(doctype).fields}
    return fieldname in _meta_cache[doctype]


def _try_set(doc, **kwargs):
    """Set only the fields the doctype actually has (schema-drift-proof)."""
    for k, v in kwargs.items():
        if _has_field(doc.doctype, k):
            doc.set(k, v)


# ═══════════════════════════════════════════════════════════════════════
# CLEARING
# ═══════════════════════════════════════════════════════════════════════

# Tables wiped without condition, children first. These are all data
# tables; masters/settings (Item Group, Company, SaathiMart Settings,
# SM Feature Flag, the seven static-page Singles) are intentionally kept.
WIPE_TABLES = [
    # order flow
    "Order Tax", "Order Item", "Order Event Log", "Order",
    "Cart Item", "Cart",
    "Payment Log", "Stock Ledger Entry", "Vendor Payout",
    "Webhook Event", "Dead Letter Log",
    # marketing / loyalty / membership
    "Coupon Usage", "Coupon",
    "Loyalty Point Entry", "Membership Saving Entry", "Customer Membership",
    "Loyalty Program", "Membership Plan",
    # engagement
    "Review", "Wishlist", "Address", "Pending Verification",
    "Contact Submission", "SM Notification", "SM Notification Device",
    "SM Search Term", "SM Audit Log",
    # delivery
    "Delivery Slot Booking", "Delivery Time Slot", "Delivery Zone",
    # catalog
    "Vendor Stock", "Vendor Listing", "Vendor Barcode Index",
    "Product Variant Attribute", "Product Specification", "Product Price",
    "Product Media", "Product",
    "Category", "Brand",
    # CMS
    "Hero Slide", "Banner", "Seasonal Banner", "Trust Badge",
    "Popular Location", "Navigation Item", "Product Rail Heading",
    "Blog Post", "FAQ Item", "FAQ Category", "Website Content",
    "Site Page", "Offer",
]


def clear_all(keep_vendors=None):
    """Wipe all transactional/demo data. DB-level deletes: dev reset only.

    Keeps: the real site-vendor rows (+ their warehouse children and
    secrets), SaathiMart Settings, SM Feature Flag, static-page Singles.
    """
    keep_vendors = keep_vendors or DEFAULT_SITE_VENDORS
    print(f"Clearing all demo data (keeping vendors: {', '.join(keep_vendors)})...")

    for dt in WIPE_TABLES:
        try:
            n = frappe.db.count(dt)
            if n:
                frappe.db.sql(f"DELETE FROM `tab{dt}`")  # nosemgrep
                print(f"  {dt}: {n} deleted")
        except Exception as e:
            print(f"  {dt}: SKIP ({str(e)[:60]})")

    # Vendors: everything except the real site vendors.
    ph = ", ".join(["%s"] * len(keep_vendors))
    n = frappe.db.sql(
        f"SELECT COUNT(*) FROM tabVendor WHERE name NOT IN ({ph})",  # nosemgrep
        keep_vendors,
    )[0][0]
    frappe.db.sql(  # nosemgrep
        f"DELETE FROM tabVendor WHERE name NOT IN ({ph})", keep_vendors
    )
    frappe.db.sql(  # nosemgrep
        f"DELETE FROM `tabVendor Warehouse` WHERE parent NOT IN ({ph})",
        keep_vendors,
    )
    print(f"  Vendor: {n} deleted, {len(keep_vendors)} kept")

    frappe.db.commit()
    print("Clear complete.")
    return {"wiped": len(WIPE_TABLES) + 1}


# ═══════════════════════════════════════════════════════════════════════
# VENDORS
# ═══════════════════════════════════════════════════════════════════════

def ensure_site_vendors(sites=None):
    """Make sure every real site vendor has an Active Vendor row.

    On a live stack these rows already exist (created at registration,
    holding the webhook secrets) — this only tops up missing ones so a
    truly fresh environment can seed too.
    """
    sites = sites or DEFAULT_SITE_VENDORS
    out = []
    geo = {
        "vendor1.localhost": (27.7172, 85.3240),
        "vendor2.localhost": (27.7060, 85.3100),
        "vendor3.localhost": (27.6900, 85.3500),
        "vendor4.localhost": (27.7300, 85.3400),
    }
    for site in sites:
        if frappe.db.exists("Vendor", site):
            if frappe.db.get_value("Vendor", site, "status") != "Active":
                frappe.db.set_value("Vendor", site, "status", "Active")
            out.append(site)
            continue
        lat, lng = geo.get(site, (27.7172, 85.3240))
        v = frappe.new_doc("Vendor")
        _try_set(
            v,
            vendor_name=site.split(".")[0].replace("vendor", "Vendor ").title(),
            status="Active",
            lat=lat,
            lng=lng,
            service_radius_km=10,
            contact_email=f"admin@{site}",
            contact_phone=f"98{random.randint(10000000, 99999999)}",
        )
        v.flags.ignore_permissions = True
        v.flags.ignore_links = True
        v.insert(ignore_permissions=True)
        if _has_field("Vendor", "warehouses"):
            v.append(
                "warehouses",
                {"warehouse_name": "default", "is_default": 1, "lat": lat, "lng": lng},
            )
            v.save(ignore_permissions=True)
        out.append(v.name)
    frappe.db.commit()
    return out


# ═══════════════════════════════════════════════════════════════════════
# CATALOG
# ═══════════════════════════════════════════════════════════════════════

def seed_categories():
    categories = []
    cat_data = [
        ("Dairy & Bakery", "dairy-bakery"),
        ("Fruits & Vegetables", "fruits-vegetables"),
        ("Snacks & Beverages", "snacks-beverages"),
        ("Personal Care", "personal-care"),
        ("Household Essentials", "household-essentials"),
        ("Staples", "staples"),
    ]
    for category_name, slug in cat_data:
        existing = frappe.db.exists("Category", {"slug": slug})
        if existing:
            categories.append(existing)
            continue
        cat = frappe.new_doc("Category")
        _try_set(cat, category_name=category_name, slug=slug, is_active=1)
        cat.flags.ignore_permissions = True
        cat.insert(ignore_permissions=True)
        categories.append(cat.name)
    frappe.db.commit()
    return categories


def seed_brands():
    brands = []
    for brand_name in [
        "Head & Shoulders", "Dove", "Sunsilk", "Clinic Plus", "Himalaya",
        "Patanjali", "Colgate", "Dettol", "Lifebuoy", "Nivea", "Amul",
        "Nepal Dairy", "Tiger Brand", "Wai Wai", "Coca-Cola", "Tide",
        "Surf Excel", "Harpic", "Dabur", "Everest",
    ]:
        slug = frappe.scrub(brand_name).replace("_", "-")
        if frappe.db.exists("Brand", {"slug": slug}):
            brands.append(frappe.db.get_value("Brand", {"slug": slug}, "name"))
            continue
        b = frappe.new_doc("Brand")
        _try_set(b, brand_name=brand_name, slug=slug, is_active=1)
        b.flags.ignore_permissions = True
        b.insert(ignore_permissions=True)
        brands.append(b.name)
    frappe.db.commit()
    return brands


# (name, category slug, base price NPR, unit, brand) — a real mart shelf.
BASE_ITEMS = [
    # Personal care — the shampoo aisle leads
    ("Head & Shoulders Shampoo", "personal-care", 175, "180ml", "Head & Shoulders"),
    ("Sunsilk Shampoo", "personal-care", 145, "175ml", "Sunsilk"),
    ("Clinic Plus Shampoo", "personal-care", 130, "175ml", "Clinic Plus"),
    ("Himalaya Anti-Hairfall Shampoo", "personal-care", 210, "200ml", "Himalaya"),
    ("Dove Shampoo", "personal-care", 240, "180ml", "Dove"),
    ("Patanjali Aloe Vera Shampoo", "personal-care", 95, "100ml", "Patanjali"),
    ("Dove Soap", "personal-care", 45, "100g", "Dove"),
    ("Lifebuoy Soap", "personal-care", 40, "100g", "Lifebuoy"),
    ("Dettol Soap", "personal-care", 55, "100g", "Dettol"),
    ("Dettol Handwash", "personal-care", 99, "250ml", "Dettol"),
    ("Colgate Toothpaste", "personal-care", 85, "150g", "Colgate"),
    ("Patanjali Toothpaste", "personal-care", 75, "100g", "Patanjali"),
    ("Nivea Cream", "personal-care", 190, "100ml", "Nivea"),
    ("Himalaya Face Wash", "personal-care", 165, "100ml", "Himalaya"),
    ("Clinic Plus Conditioner", "personal-care", 150, "175ml", "Clinic Plus"),
    # Dairy & bakery
    ("Fresh Milk", "dairy-bakery", 85, "1L", "Nepal Dairy"),
    ("Amul Butter", "dairy-bakery", 120, "200g", "Amul"),
    ("White Bread", "dairy-bakery", 45, "400g", "Tiger Brand"),
    ("Farm Fresh Eggs", "dairy-bakery", 150, "(12)", "Nepal Dairy"),
    ("Yogurt", "dairy-bakery", 60, "500g", "Nepal Dairy"),
    ("Cheese Slices", "dairy-bakery", 180, "(10)", "Amul"),
    ("Paneer", "dairy-bakery", 160, "200g", "Amul"),
    ("Frozen Momo", "dairy-bakery", 180, "(10 pc)", "Tiger Brand"),
    # Fruits & vegetables
    ("Apple", "fruits-vegetables", 180, "1kg", ""),
    ("Banana", "fruits-vegetables", 60, "1kg", ""),
    ("Tomato", "fruits-vegetables", 50, "1kg", ""),
    ("Potato", "fruits-vegetables", 40, "1kg", ""),
    ("Onion", "fruits-vegetables", 55, "1kg", ""),
    ("Carrot", "fruits-vegetables", 70, "1kg", ""),
    ("Cabbage", "fruits-vegetables", 45, "pc", ""),
    ("Orange", "fruits-vegetables", 120, "1kg", ""),
    ("Grapes", "fruits-vegetables", 220, "500g", ""),
    ("Mango", "fruits-vegetables", 250, "1kg", ""),
    ("Coriander", "fruits-vegetables", 20, "100g", ""),
    # Snacks & beverages
    ("Wai Wai Noodles", "snacks-beverages", 30, "75g", "Wai Wai"),
    ("Mayos Noodles", "snacks-beverages", 30, "75g", "Wai Wai"),
    ("Coca-Cola", "snacks-beverages", 65, "1L", "Coca-Cola"),
    ("Sprite", "snacks-beverages", 65, "1L", "Coca-Cola"),
    ("Fanta", "snacks-beverages", 65, "1L", "Coca-Cola"),
    ("Tropicana Juice", "snacks-beverages", 95, "1L", ""),
    ("Potato Chips", "snacks-beverages", 35, "52g", ""),
    ("Parle-G Biscuits", "snacks-beverages", 15, "80g", ""),
    ("Chiwda", "snacks-beverages", 45, "200g", ""),
    ("Churpi", "snacks-beverages", 60, "100g", ""),
    # Household
    ("Tide Detergent", "household-essentials", 145, "1kg", "Tide"),
    ("Surf Excel Detergent", "household-essentials", 155, "1kg", "Surf Excel"),
    ("Vim Dishwash Bar", "household-essentials", 30, "200g", ""),
    ("Vim Dishwash Liquid", "household-essentials", 75, "500ml", ""),
    ("Harpic Toilet Cleaner", "household-essentials", 130, "500ml", "Harpic"),
    ("Lizol Floor Cleaner", "household-essentials", 120, "500ml", ""),
    ("Colin Glass Cleaner", "household-essentials", 85, "250ml", ""),
    ("Paper Napkins", "household-essentials", 55, "(100)", ""),
    ("Garbage Bags", "household-essentials", 90, "(30)", ""),
    # Staples
    ("Basmati Rice", "staples", 320, "5kg", ""),
    ("Jeera Masino Rice", "staples", 280, "5kg", "Everest"),
    ("Sunflower Oil", "staples", 280, "1L", ""),
    ("Mustard Oil", "staples", 260, "1L", ""),
    ("Red Lentils", "staples", 150, "1kg", ""),
    ("Chickpeas", "staples", 140, "1kg", ""),
    ("Sugar", "staples", 90, "1kg", ""),
    ("Salt", "staples", 25, "1kg", ""),
    ("Tea Leaves", "staples", 240, "500g", ""),
    ("Coffee Powder", "staples", 350, "200g", ""),
    ("Turmeric Powder", "staples", 45, "100g", "Everest"),
    ("Sel Roti Mix", "staples", 120, "1kg", ""),
]


def seed_products(categories, count=200):
    """Create mart products with per-vendor listings AND per-vendor stock.

    Stock truth lives on Vendor Stock (checkout reserves from it);
    Vendor Listing carries the storefront price. Each product is listed
    by 1-3 active vendors with its own price and stock pool.
    """
    products = []

    product_data = []
    seen = set()
    for name, cat, price, unit, brand in BASE_ITEMS:
        for variant, mult in (("", 1.0), (" Family Pack", 2.5), (" Combo", 4.0)):
            if len(product_data) >= count:
                break
            pname = f"{name} {unit}{variant}".strip()
            if pname in seen:
                continue
            seen.add(pname)
            product_data.append(
                {
                    "product_name": pname,
                    "category": cat,
                    "price": round(price * mult, 0),
                    "brand": brand,
                }
            )
        if len(product_data) >= count:
            break

    # Top up with numbered value packs if still short (idempotent names).
    i = 1
    while len(product_data) < count:
        pname = f"Mart Value Pack #{i}"
        if pname not in seen:
            seen.add(pname)
            product_data.append(
                {
                    "product_name": pname,
                    "category": "household-essentials",
                    "price": 100 + i * 5,
                    "brand": "",
                }
            )
        i += 1

    vendors = frappe.get_all("Vendor", filters={"status": "Active"}, pluck="name")
    if not vendors:
        print("  No active vendors — skipping product seeding")
        return products

    for idx, pdata in enumerate(product_data):
        # Product autonames by field:product_name (with -N suffixes for
        # duplicates) — key idempotency on product_name to keep re-seeds
        # from piling up "...-1" copies.
        existing = frappe.db.exists("Product", {"product_name": pdata["product_name"]})
        if existing:
            products.append(existing)
            continue

        product = frappe.new_doc("Product")
        product.product_name = pdata["product_name"]
        product.slug = frappe.scrub(pdata["product_name"]).replace("_", "-")
        product.status = "Active"
        _try_set(
            product,
            category=pdata["category"],
            brand=pdata["brand"] or None,
            sku=f"SM-{100000 + idx * 7}",
            barcode=f"890{1000000000 + idx}",  # deterministic EAN-style code
            short_description=f"{pdata['product_name']} — fresh from local vendors",
            track_inventory=1,
        )
        product.flags.ignore_permissions = True
        product.insert(ignore_permissions=True)

        # 1-3 vendors list each product; stock truth goes on Vendor Stock.
        num_vendors = random.randint(1, min(3, len(vendors)))
        selected_vendors = random.sample(vendors, num_vendors)

        for j, vendor in enumerate(selected_vendors):
            vendor_price = pdata["price"] * random.uniform(0.9, 1.1)
            qty = random.randint(15, 120)

            vl = frappe.new_doc("Vendor Listing")
            _try_set(
                vl,
                product=product.name,
                vendor=vendor,
                price=round(vendor_price, 2),
                compare_price=round(vendor_price * 1.2, 2) if random.random() > 0.5 else 0,
                track_inventory=1,
                status="Active",
                priority=10 - j,
                warehouse="default",
            )
            vl.flags.ignore_permissions = True
            vl.insert(ignore_permissions=True)

            vs = frappe.new_doc("Vendor Stock")
            _try_set(
                vs,
                product=product.name,
                vendor=vendor,
                warehouse="default",
                is_default_warehouse=1,
                physical_qty=qty,
                available_qty=qty,
                reserved_qty=0,
            )
            vs.flags.ignore_permissions = True
            vs.insert(ignore_permissions=True)

        products.append(product.name)

    frappe.db.commit()
    return products


# ═══════════════════════════════════════════════════════════════════════
# STOREFRONT (zones, slots, payment modes, coupons, loyalty, CMS)
# ═══════════════════════════════════════════════════════════════════════

def seed_storefront():
    """Seed everything the storefront needs to look and work alive."""
    out = {}

    # Delivery zones (Kathmandu ring)
    zones = [
        ("Kathmandu Central", "Kathmandu", 100, 1500),
        ("Kathmandu Outer", "Kathmandu", 130, 2000),
        ("Lalitpur", "Lalitpur", 110, 1500),
        ("Bhaktapur", "Bhaktapur", 140, 2000),
    ]
    out["zones"] = []
    for zone_name, city, charge, free_above in zones:
        if frappe.db.exists("Delivery Zone", {"zone_name": zone_name}):
            out["zones"].append(frappe.db.get_value("Delivery Zone", {"zone_name": zone_name}, "name"))
            continue
        z = frappe.new_doc("Delivery Zone")
        _try_set(
            z,
            zone_name=zone_name,
            city=city,
            is_active=1,
            delivery_charge=charge,
            base_delivery_charge=charge,
            free_delivery_above=free_above,
            estimated_days=1,
            loyalty_multiplier=1.0,
            first_order_discount_pct=10,
            second_order_discount_pct=5,
            onboarding_max_discount_amount=200,
        )
        z.flags.ignore_permissions = True
        z.insert(ignore_permissions=True)
        out["zones"].append(z.name)

    # Delivery time slots (fields: slot_name/start_time/end_time/max_orders)
    out["slots"] = []
    if frappe.db.count("Delivery Time Slot") == 0:
        for label, start, end, cap in [
            ("Morning", "08:00", "12:00", 20),
            ("Afternoon", "12:00", "16:00", 20),
            ("Evening", "16:00", "20:00", 25),
        ]:
            s = frappe.new_doc("Delivery Time Slot")
            _try_set(
                s,
                slot_name=label,
                start_time=start,
                end_time=end,
                max_orders=cap,
                is_active=1,
            )
            s.flags.ignore_permissions = True
            s.insert(ignore_permissions=True)
            out["slots"].append(s.name)

    # Payment modes — gateway must match the doctype's Select options
    # (empty or "eSewa"; more gateways land as they get integrated).
    out["payment_modes"] = []
    for mode_name, is_online, gateway, disp in [
        ("Cash on Delivery", 0, "", 1),
        ("eSewa", 1, "eSewa", 2),
    ]:
        if frappe.db.exists("Payment Mode", {"mode_name": mode_name}):
            out["payment_modes"].append(frappe.db.get_value("Payment Mode", {"mode_name": mode_name}, "name"))
            continue
        pm = frappe.new_doc("Payment Mode")
        _try_set(
            pm,
            mode_name=mode_name,
            slug=frappe.scrub(mode_name).replace("_", "-"),
            is_enabled=1,
            is_online=is_online,
            gateway=gateway,
            display_order=disp,
            description=f"Pay with {mode_name}",
        )
        pm.flags.ignore_permissions = True
        pm.insert(ignore_permissions=True)
        out["payment_modes"].append(pm.name)

    # Coupons — coupon_type/absorption_type must match the doctype's
    # Select options ("Percentage"/"Fixed Amount" and "Platform"/"Vendor").
    out["coupons"] = []
    for code, ctype, pct, amt, min_order, max_disc, absorption in [
        ("CHECKOUT10", "Percentage", 10, 0, 500, 300, "Platform"),
        ("SAVE50", "Fixed Amount", 0, 50, 300, 0, "Platform"),
        ("FRESH15", "Percentage", 15, 0, 1000, 500, "Vendor"),
        ("MART100", "Fixed Amount", 0, 100, 1500, 0, "Vendor"),
    ]:
        if frappe.db.exists("Coupon", {"coupon_code": code}):
            out["coupons"].append(frappe.db.get_value("Coupon", {"coupon_code": code}, "name"))
            continue
        c = frappe.new_doc("Coupon")
        _try_set(
            c,
            coupon_code=code,
            coupon_type=ctype,
            is_active=1,
            discount_percentage=pct,
            discount_amount=amt,
            min_order_amount=min_order,
            max_discount_amount=max_disc,
            max_uses=1000,
            max_uses_per_user=5,
            valid_from=add_days(today(), -7),
            valid_to=add_days(today(), 90),
            absorption_type=absorption,
        )
        c.flags.ignore_permissions = True
        c.insert(ignore_permissions=True)
        out["coupons"].append(c.name)

    # Loyalty program
    if not frappe.db.count("Loyalty Program"):
        lp = frappe.new_doc("Loyalty Program")
        _try_set(
            lp,
            program_name="SaathiMart Rewards",
            is_active=1,
            collection_factor=0.01,  # 1 point per NPR 100 spent
            redemption_factor=1.0,
            min_points_to_redeem=100,
            max_redemption_per_order_pct=20,
            point_expiry_days=365,
        )
        lp.flags.ignore_permissions = True
        lp.insert(ignore_permissions=True)
        out["loyalty_program"] = lp.name

    # Membership plan
    if not frappe.db.count("Membership Plan"):
        mp = frappe.new_doc("Membership Plan")
        _try_set(
            mp,
            plan_name="SaathiMart Plus",
            is_active=1,
            tagline="Free delivery all month",
            price=499,
            duration_days=30,
            free_delivery=1,
            free_delivery_min_order=200,
            max_discount_per_order=200,
            sort_order=1,
        )
        mp.flags.ignore_permissions = True
        mp.insert(ignore_permissions=True)
        out["membership_plan"] = mp.name

    # Hero slides
    out["hero_slides"] = []
    if frappe.db.count("Hero Slide") == 0:
        for i, (title, desc, cta) in enumerate(
            [
                ("Fresh Groceries|Delivered Fast", "Shampoo to staples from stores near you", "Shop now"),
                ("Monsoon Personal Care|Up to 25% off", "Shampoos, soaps and more", "Grab the deal"),
                ("Membership|Free delivery forever", "SaathiMart Plus from NPR 499/month", "Join now"),
            ],
            start=1,
        ):
            h = frappe.new_doc("Hero Slide")
            _try_set(
                h,
                slide_key=f"hero-{i}",
                title_lines=title,
                description=desc,
                cta_label=cta,
                cta_href="/shop",
                sort_order=i,
                published=1,
            )
            h.flags.ignore_permissions = True
            h.insert(ignore_permissions=True)
            out["hero_slides"].append(h.name)

    # Trust badges
    out["trust_badges"] = []
    if frappe.db.count("Trust Badge") == 0:
        for i, (icon, title, desc) in enumerate(
            [
                ("delivery", "Fast Delivery", "Under 2 hours in the valley"),
                ("authentic", "100% Genuine", "Sourced from verified vendors"),
                ("hours", "Best Prices", "Compare across local stores"),
                ("free-delivery", "Free Delivery", "On orders above the zone threshold"),
            ],
            start=1,
        ):
            tb = frappe.new_doc("Trust Badge")
            _try_set(
                tb, icon_key=icon, title=title, description=desc,
                sort_order=i, published=1,
            )
            tb.flags.ignore_permissions = True
            tb.insert(ignore_permissions=True)
            out["trust_badges"].append(tb.name)

    # Popular locations
    out["popular_locations"] = []
    for loc, city, district, lat, lng in [
        ("Baneshwor", "Kathmandu", "Kathmandu", 27.6895, 85.3370),
        ("Thamel", "Kathmandu", "Kathmandu", 27.7154, 85.3123),
        ("Lakeside", "Pokhara", "Kaski", 28.2096, 83.9587),
        ("Jhamsikhel", "Lalitpur", "Lalitpur", 27.6740, 85.3100),
    ]:
        slug = frappe.scrub(loc).replace("_", "-")
        if frappe.db.exists("Popular Location", {"slug": slug}):
            out["popular_locations"].append(frappe.db.get_value("Popular Location", {"slug": slug}, "name"))
            continue
        pl = frappe.new_doc("Popular Location")
        _try_set(
            pl,
            location_name=loc,
            slug=slug,
            city=city,
            district=district,
            latitude=lat,
            longitude=lng,
            sort_order=len(out["popular_locations"]) + 1,
            is_active=1,
        )
        pl.flags.ignore_permissions = True
        pl.insert(ignore_permissions=True)
        out["popular_locations"].append(pl.name)

    # Offers
    out["offers"] = []
    for title, subtitle, code, status in [
        ("Monsoon Care Fest", "Up to 25% off shampoos & soaps", "CHECKOUT10", "Published"),
        ("Staples Bonanza", "Rice, oil and lentils at honest prices", "SAVE50", "Published"),
        ("New Member Deal", "Extra 10% off your second order", "FRESH15", "Published"),
    ]:
        slug = frappe.scrub(title).replace("_", "-")
        if frappe.db.exists("Offer", {"slug": slug}):
            out["offers"].append(frappe.db.get_value("Offer", {"slug": slug}, "name"))
            continue
        o = frappe.new_doc("Offer")
        _try_set(
            o,
            title=title,
            slug=slug,
            subtitle=subtitle,
            status=status,
            is_active=1,
            sort_order=len(out["offers"]) + 1,
            valid_from=add_days(today(), -1),
            valid_to=add_days(today(), 60),
            coupon_code=code,
            description=f"{title} — {subtitle}",
        )
        o.flags.ignore_permissions = True
        o.insert(ignore_permissions=True)
        out["offers"].append(o.name)

    # Navigation items (fields: label/url/menu_location/sort_order/is_active)
    out["nav_items"] = []
    if frappe.db.count("Navigation Item") == 0:
        for i, (label, href, group) in enumerate(
            [
                ("Shop", "/shop", "Header"),
                ("Categories", "/categories", "Header"),
                ("Offers", "/offers", "Header"),
                ("About Us", "/about", "Footer"),
                ("Contact", "/contact", "Footer"),
                ("Privacy Policy", "/privacy", "Footer"),
            ],
            start=1,
        ):
            n = frappe.new_doc("Navigation Item")
            _try_set(
                n, label=label, url=href, menu_location=group,
                sort_order=i, is_active=1,
            )
            n.flags.ignore_permissions = True
            n.insert(ignore_permissions=True)
            out["nav_items"].append(n.name)

    # Product rail headings (rail_key Select: featured/personal-care/
    # dairy-bakery/cleaning-household)
    out["rail_headings"] = []
    if frappe.db.count("Product Rail Heading") == 0:
        for i, (heading, rail_key) in enumerate(
            [
                ("Featured Picks", "featured"),
                ("Personal Care Picks", "personal-care"),
                ("Dairy & Bakery Fresh", "dairy-bakery"),
                ("Cleaning & Household", "cleaning-household"),
            ],
            start=1,
        ):
            r = frappe.new_doc("Product Rail Heading")
            _try_set(
                r, rail_key=rail_key, title=heading,
                sort_order=i, published=1,
            )
            r.flags.ignore_permissions = True
            r.insert(ignore_permissions=True)
            out["rail_headings"].append(r.name)

    # FAQ
    out["faq"] = []
    if frappe.db.count("FAQ Item") == 0 and not frappe.db.count("FAQ Category"):
        fc = frappe.new_doc("FAQ Category")
        _try_set(fc, category_name="Orders & Delivery", slug="orders-delivery", is_active=1, sort_order=1)
        fc.flags.ignore_permissions = True
        fc.insert(ignore_permissions=True)
        out["faq"].append(fc.name)
        for i, (q, a) in enumerate(
            [
                ("How fast is delivery?", "Most orders arrive within 2 hours inside the ring road."),
                ("Can I pay cash on delivery?", "Yes — COD is available in all zones."),
                ("How do I return an item?", "Hand it back to the rider within 24 hours for an instant refund."),
            ],
            start=1,
        ):
            f = frappe.new_doc("FAQ Item")
            _try_set(
                f, question=q, answer=a, category=fc.name, sort_order=i,
                is_active=1, published=1,
            )
            f.flags.ignore_permissions = True
            f.insert(ignore_permissions=True)
            out["faq"].append(f.name)

    # Blog post
    out["blog"] = []
    if frappe.db.count("Blog Post") == 0:
        bp = frappe.new_doc("Blog Post")
        _try_set(
            bp,
            title="Welcome to SaathiMart",
            slug="welcome-to-saathimart",
            author="SaathiMart Team",
            excerpt="Your neighbourhood stores, one delivery away.",
            content="Your neighbourhood stores, one delivery away. "
            "SaathiMart connects local shops with shoppers across the valley.",
            status="Published",
        )
        bp.flags.ignore_permissions = True
        bp.insert(ignore_permissions=True)
        out["blog"].append(bp.name)

    frappe.db.commit()
    return out


# ═══════════════════════════════════════════════════════════════════════
# ORDERS (demo history)
# ═══════════════════════════════════════════════════════════════════════

def seed_orders(vendors, products, count=12):
    """Create sample historical orders (direct inserts, not checkout)."""
    orders = []

    zone = frappe.db.get_value("Delivery Zone", {"zone_name": "Kathmandu Central"}, "name")
    customer_names = [
        "Ram Shrestha", "Sita Gurung", "Hari Thapa", "Gita Magar",
        "Krishna Tamang", "Laxmi Rai", "Shyam Poudel", "Sarita Karki",
    ]
    statuses = ["Pending", "Confirmed", "Preparing", "Out for Delivery", "Delivered"]

    for i in range(count):
        customer_name = random.choice(customer_names)
        vendor = random.choice(vendors)
        num_items = random.randint(1, 5)
        selected_products = random.sample(products, min(num_items, len(products)))

        order = frappe.new_doc("Order")
        _try_set(
            order,
            customer_name=customer_name,
            customer_phone=f"98{random.randint(10000000, 99999999)}",
            customer_email=f"{customer_name.lower().replace(' ', '.')}@demo.com",
            delivery_address=f"House {random.randint(1, 100)}, Kathmandu, Nepal",
            delivery_zone=zone,
            payment_method=random.choice(["COD", "eSewa"]),
            status=random.choice(statuses),
            vendor=vendor,
            notes=f"Demo order #{i + 1}",
        )
        order.payment_status = "Paid" if order.status in ("Delivered", "Preparing") else "Unpaid"

        grand_total = 0
        for prod_name in selected_products:
            product_doc = frappe.get_doc("Product", prod_name)
            vl_price = frappe.db.get_value(
                "Vendor Listing",
                {"product": prod_name, "vendor": vendor, "status": "Active"},
                "price",
            )
            price = vl_price or flt_price(product_doc)
            qty = random.randint(1, 3)
            amount = price * qty
            grand_total += amount
            order.append(
                "items",
                {
                    "product": prod_name,
                    "product_name": product_doc.product_name,
                    "qty": qty,
                    "rate": price,
                    "amount": amount,
                    "vendor": vendor,
                },
            )

        order.grand_total = grand_total
        order.flags.ignore_permissions = True
        order.insert(ignore_permissions=True)

        days_ago = random.randint(0, 6)
        frappe.db.set_value("Order", order.name, "creation", add_days(now_datetime(), -days_ago))
        orders.append(order.name)

    frappe.db.commit()
    return orders


def flt_price(product_doc):
    """Fallback price: cheapest active listing for the product."""
    return frappe.db.get_value(
        "Vendor Listing", {"product": product_doc.name, "status": "Active"},
        "price", order_by="price asc",
    ) or 0


# ═══════════════════════════════════════════════════════════════════════
# ENTRY POINTS
# ═══════════════════════════════════════════════════════════════════════

def seed_all(count=200, with_orders=12):
    """Create all demo data: vendors, catalog, storefront, orders."""
    print("Seeding demo data...")

    vendors = ensure_site_vendors()
    print(f"  Vendors ready: {len(vendors)}")

    categories = seed_categories()
    print(f"  Created/kept {len(categories)} categories")

    seed_brands()
    print("  Brands seeded")

    products = seed_products(categories, count=count)
    print(f"  Created/kept {len(products)} products")

    storefront = seed_storefront()
    print(f"  Storefront seeded: {sorted(storefront.keys())}")

    orders = seed_orders(vendors, products, count=with_orders) if with_orders else []
    if orders:
        print(f"  Created {len(orders)} demo orders")

    print("Seeding complete!")
    return {
        "vendors": len(vendors),
        "products": len(products),
        "orders": len(orders),
        "storefront": storefront,
    }
