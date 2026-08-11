"""
Seed script — inserts realistic Nepal grocery/FMCG data into SaathiMart.

Run inside bench:
    bench --site <site> execute saathimart.scripts.seed.run
"""
import json

import frappe
from frappe.utils import today, add_days


def run():
    frappe.set_user("Administrator")
    _seed_settings()
    _seed_delivery_zones()
    _seed_vendors()
    _seed_categories()
    _seed_products()
    _seed_loyalty_program()
    _seed_coupons()
    _seed_site_config()
    _seed_banners()
    _seed_navigation()
    _seed_pages()
    _seed_blog_posts()
    frappe.db.commit()
    print("✅ SaathiMart seed complete")


# ── Settings ──────────────────────────────────────────────────────────────────

def _seed_settings():
    s = frappe.get_single("Settings")
    s.site_name            = "SaathiMart"
    s.currency             = "NPR"
    s.enable_esewa         = 1
    s.esewa_merchant_code  = "EPAYTEST"
    s.payment_sandbox_mode = 1
    s.enable_loyalty       = 1
    s.enable_coupons       = 1
    s.min_order_amount     = 200
    s.free_delivery_above  = 1500
    s.cart_expiry_days     = 7
    s.abandoned_cart_hours = 24
    s.save(ignore_permissions=True)
    print("  settings saved")


# ── Delivery Zones ────────────────────────────────────────────────────────────

ZONES = [
    {"zone_name": "Kathmandu Valley",  "city": "Kathmandu",  "districts": "Kathmandu,Lalitpur,Bhaktapur",
     "delivery_charge": 80,  "free_delivery_above": 1500, "estimated_days": 1},
    {"zone_name": "Pokhara",           "city": "Pokhara",    "districts": "Kaski",
     "delivery_charge": 150, "free_delivery_above": 2000, "estimated_days": 2},
    {"zone_name": "Chitwan",           "city": "Bharatpur",  "districts": "Chitwan",
     "delivery_charge": 150, "free_delivery_above": 2000, "estimated_days": 2},
    {"zone_name": "Biratnagar",        "city": "Biratnagar", "districts": "Morang",
     "delivery_charge": 200, "free_delivery_above": 2500, "estimated_days": 3},
    {"zone_name": "Outside Valley",    "city": "",           "districts": "",
     "delivery_charge": 250, "free_delivery_above": 3000, "estimated_days": 4},
]

def _seed_delivery_zones():
    for z in ZONES:
        if frappe.db.exists("Delivery Zone", z["zone_name"]):
            continue
        doc = frappe.new_doc("Delivery Zone")
        doc.update(z)
        doc.is_active = 1
        doc.insert(ignore_permissions=True)
    print(f"  {len(ZONES)} delivery zones")


# ── Vendors ───────────────────────────────────────────────────────────────────

VENDORS = [
    {"vendor_name": "Fresh Mart Nepal",   "contact_email": "fresh@saathimart.np",  "contact_phone": "9801234567"},
    {"vendor_name": "Himalayan Organics", "contact_email": "organic@saathimart.np","contact_phone": "9807654321"},
    {"vendor_name": "Daily Needs Store",  "contact_email": "daily@saathimart.np",  "contact_phone": "9812345678"},
]

def _seed_vendors():
    for v in VENDORS:
        if frappe.db.exists("Vendor", {"vendor_name": v["vendor_name"]}):
            continue
        doc = frappe.new_doc("Vendor")
        doc.update(v)
        doc.status = "Active"
        doc.commission_pct = 10
        doc.insert(ignore_permissions=True)
    print(f"  {len(VENDORS)} vendors")


# ── Categories ────────────────────────────────────────────────────────────────

CATEGORIES = [
    {"category_name": "Vegetables",    "slug": "vegetables",    "sort_order": 1},
    {"category_name": "Fruits",        "slug": "fruits",        "sort_order": 2},
    {"category_name": "Dairy & Eggs",  "slug": "dairy-and-eggs", "sort_order": 3},
    {"category_name": "Grains & Rice", "slug": "grains-rice",   "sort_order": 4},
    {"category_name": "Spices",        "slug": "spices",        "sort_order": 5},
    {"category_name": "Beverages",     "slug": "beverages",     "sort_order": 6},
    {"category_name": "Snacks",        "slug": "snacks",        "sort_order": 7},
    {"category_name": "Household",     "slug": "household",     "sort_order": 8},
]

def _seed_categories():
    for c in CATEGORIES:
        if frappe.db.exists("Category", {"slug": c["slug"]}):
            continue
        doc = frappe.new_doc("Category")
        doc.update(c)
        doc.is_active = 1
        doc.insert(ignore_permissions=True)
    print(f"  {len(CATEGORIES)} categories")


# ── Products ──────────────────────────────────────────────────────────────────

PRODUCTS = [
    # Vegetables
    {"product_name": "Tomato (1 kg)",    "slug": "tomato-1kg",    "category": "vegetables",
     "price": 80,  "compare_price": 100, "sku": "VEG-TOM-1KG", "stock_qty": 200,
     "short_description": "Fresh local tomatoes",
     "prices": [
         {"price_type": "Retail",    "price": 80,  "min_qty": 1},
         {"price_type": "Wholesale", "price": 65,  "min_qty": 10},
         {"price_type": "B2B",       "price": 55,  "min_qty": 50},
     ]},
    {"product_name": "Potato (1 kg)",    "slug": "potato-1kg",    "category": "vegetables",
     "price": 60,  "compare_price": 75,  "sku": "VEG-POT-1KG", "stock_qty": 500,
     "short_description": "Himalayan potatoes",
     "prices": [
         {"price_type": "Retail",    "price": 60,  "min_qty": 1},
         {"price_type": "Wholesale", "price": 48,  "min_qty": 10},
         {"price_type": "B2B",       "price": 40,  "min_qty": 50},
     ]},
    {"product_name": "Onion (1 kg)",     "slug": "onion-1kg",     "category": "vegetables",
     "price": 70,  "sku": "VEG-ONI-1KG", "stock_qty": 300,
     "prices": [
         {"price_type": "Retail",    "price": 70,  "min_qty": 1},
         {"price_type": "Wholesale", "price": 58,  "min_qty": 10},
     ]},
    # Fruits
    {"product_name": "Banana (dozen)",   "slug": "banana-dozen",  "category": "fruits",
     "price": 120, "sku": "FRT-BAN-DOZ", "stock_qty": 150,
     "prices": [
         {"price_type": "Retail",    "price": 120, "min_qty": 1},
         {"price_type": "Wholesale", "price": 95,  "min_qty": 5},
     ]},
    {"product_name": "Apple (1 kg)",     "slug": "apple-1kg",     "category": "fruits",
     "price": 350, "compare_price": 400, "sku": "FRT-APL-1KG", "stock_qty": 80,
     "prices": [
         {"price_type": "Retail",    "price": 350, "min_qty": 1},
         {"price_type": "Wholesale", "price": 290, "min_qty": 5},
     ]},
    # Dairy
    {"product_name": "Milk (1 litre)",   "slug": "milk-1l",       "category": "dairy-and-eggs",
     "price": 95,  "sku": "DAI-MLK-1L",  "stock_qty": 400,
     "prices": [
         {"price_type": "Retail",    "price": 95,  "min_qty": 1},
         {"price_type": "Wholesale", "price": 85,  "min_qty": 12},
     ]},
    {"product_name": "Eggs (dozen)",     "slug": "eggs-dozen",    "category": "dairy-and-eggs",
     "price": 240, "sku": "DAI-EGG-DOZ", "stock_qty": 200,
     "prices": [
         {"price_type": "Retail",    "price": 240, "min_qty": 1},
         {"price_type": "Wholesale", "price": 200, "min_qty": 5},
     ]},
    # Grains
    {"product_name": "Basmati Rice (5 kg)", "slug": "basmati-rice-5kg", "category": "grains-rice",
     "price": 850, "compare_price": 950, "sku": "GRN-RIC-5KG", "stock_qty": 120,
     "prices": [
         {"price_type": "Retail",    "price": 850, "min_qty": 1},
         {"price_type": "Wholesale", "price": 720, "min_qty": 4},
         {"price_type": "B2B",       "price": 650, "min_qty": 20},
     ]},
    {"product_name": "Wheat Flour (5 kg)", "slug": "wheat-flour-5kg", "category": "grains-rice",
     "price": 480, "sku": "GRN-FLR-5KG", "stock_qty": 200,
     "prices": [
         {"price_type": "Retail",    "price": 480, "min_qty": 1},
         {"price_type": "Wholesale", "price": 400, "min_qty": 4},
     ]},
    # Spices
    {"product_name": "Turmeric Powder (100g)", "slug": "turmeric-100g", "category": "spices",
     "price": 85,  "sku": "SPC-TUR-100G", "stock_qty": 300,
     "prices": [{"price_type": "Retail", "price": 85, "min_qty": 1}]},
    {"product_name": "Cumin Seeds (100g)",     "slug": "cumin-100g",    "category": "spices",
     "price": 120, "sku": "SPC-CUM-100G", "stock_qty": 250,
     "prices": [{"price_type": "Retail", "price": 120, "min_qty": 1}]},
    # Beverages
    {"product_name": "Wai Wai Noodles (pack of 5)", "slug": "waiwai-5pack", "category": "snacks",
     "price": 125, "sku": "SNK-WAI-5PK", "stock_qty": 500,
     "prices": [
         {"price_type": "Retail",    "price": 125, "min_qty": 1},
         {"price_type": "Wholesale", "price": 100, "min_qty": 10},
     ]},
    {"product_name": "Coca-Cola (1.5L)",  "slug": "coca-cola-1-5l", "category": "beverages",
     "price": 160, "sku": "BEV-COK-1L5", "stock_qty": 180,
     "prices": [
         {"price_type": "Retail",    "price": 160, "min_qty": 1},
         {"price_type": "Wholesale", "price": 130, "min_qty": 6},
     ]},
]

def _seed_products():
    vendors = frappe.get_all("Vendor", fields=["name"])
    count = 0
    batch = 0
    for p in PRODUCTS:
        if frappe.db.exists("Product", {"slug": p["slug"]}):
            continue
        prices = p.pop("prices", [])
        doc = frappe.new_doc("Product")
        doc.update(p)
        doc.status = "Active"
        for pr in prices:
            doc.append("prices", pr)
        doc.insert(ignore_permissions=True)
        count += 1

        for v in vendors:
            vl = frappe.new_doc("Vendor Listing")
            vl.vendor = v.name
            vl.product = doc.name
            vl.price = p["price"]
            vl.compare_price = p.get("compare_price", 0)
            vl.barcode = p["sku"]
            vl.sku = f"VEND-{p['sku']}"
            vl.status = "Active"
            vl.track_inventory = 1
            vl.allow_backorder = 0
            vl.available_qty = p.get("stock_qty", 0)
            vl.reserved_qty = 0
            vl.priority = 1
            vl.estimated_delivery_minutes = 20
            vl.insert(ignore_permissions=True)

        batch += 1
        if batch % 10 == 0:
            frappe.db.commit()
            frappe.clear_cache(doctype="Product")
            frappe.clear_cache(doctype="Vendor Listing")
            print(f"  ... {count} products committed")
    print(f"  {count} products")


# ── Loyalty Program ───────────────────────────────────────────────────────────

def _seed_loyalty_program():
    if frappe.db.exists("Loyalty Program", "SaathiMart Rewards"):
        return
    doc = frappe.new_doc("Loyalty Program")
    doc.program_name                  = "SaathiMart Rewards"
    doc.is_active                     = 1
    doc.collection_factor             = 0.01   # 1 point per NPR 100
    doc.redemption_factor             = 1.0    # 1 point = NPR 1
    doc.min_points_to_redeem          = 100
    doc.max_redemption_per_order_pct  = 20
    doc.point_expiry_days             = 365
    doc.append("tiers", {"tier_name": "Silver", "min_points": 500,  "multiplier": 1.5, "badge_color": "#94a3b8"})
    doc.append("tiers", {"tier_name": "Gold",   "min_points": 2000, "multiplier": 2.0, "badge_color": "#f59e0b"})
    doc.append("tiers", {"tier_name": "Platinum","min_points": 5000,"multiplier": 3.0, "badge_color": "#7c3aed"})
    doc.insert(ignore_permissions=True)

    # Link to settings
    s = frappe.get_single("Settings")
    s.loyalty_program = "SaathiMart Rewards"
    s.save(ignore_permissions=True)
    print("  loyalty program seeded")


# ── Coupons ───────────────────────────────────────────────────────────────────

def _seed_coupons():
    coupons = [
        {"coupon_code": "WELCOME10", "coupon_type": "Percentage", "discount_percentage": 10,
         "min_order_amount": 500, "max_uses": 1000, "max_uses_per_user": 1,
         "valid_from": today(), "valid_to": add_days(today(), 90)},
        {"coupon_code": "FLAT100",   "coupon_type": "Fixed Amount", "discount_amount": 100,
         "min_order_amount": 1000, "max_uses": 500,
         "valid_from": today(), "valid_to": add_days(today(), 30)},
        {"coupon_code": "FREEDEL",   "coupon_type": "Free Delivery",
         "min_order_amount": 800, "max_uses": 0,
         "valid_from": today(), "valid_to": add_days(today(), 60)},
    ]
    count = 0
    for c in coupons:
        if frappe.db.exists("Coupon", c["coupon_code"]):
            continue
        doc = frappe.new_doc("Coupon")
        doc.update(c)
        doc.is_active = 1
        doc.insert(ignore_permissions=True)
        count += 1
    print(f"  {count} coupons")


# ── Banners ───────────────────────────────────────────────────────────────────
# Hero (home hero carousel) and Promo Strip (home "seasonal" strip) banners.
# `heading` uses "\n" to join multi-line titles — the frontend adapter splits
# on that to build titleLines[]. image/mobile_image are left blank, which the
# frontend already treats as "use the bundled local artwork" for this slide.

BANNERS = [
    {"title": "Smart Shopping Starts Here", "banner_type": "Hero", "sort_order": 1,
     "heading": "Smart Shopping Starts Here.",
     "subheading": ("Discover thousands of quality products at unbeatable prices. "
                    "Enjoy seamless online ordering or experience the convenience "
                    "of shopping at your nearest SaathiMart."),
     "cta_label": "Shop Now", "cta_url": "/categories",
     "cta_secondary_label": "Browse Categories", "cta_secondary_url": "/categories",
     "bg_color": "#f0fdf4", "text_color": "#15803d"},
    {"title": "Dashain is Here", "banner_type": "Hero", "sort_order": 2,
     "heading": "Dashain is Here.",
     "subheading": ("Festive essentials, gifts and groceries for the whole family "
                    "— delivered fast, at honest prices."),
     "cta_label": "Shop Now", "cta_url": "/offers/dashain",
     "cta_secondary_label": "View Offers", "cta_secondary_url": "/categories",
     "bg_color": "#fef2f2", "text_color": "#b91c1c"},
    {"title": "Tihar, Made Easy", "banner_type": "Hero", "sort_order": 3,
     "heading": "Tihar, Made Easy.",
     "subheading": ("Lights, sweets and everything for the festival of lights. "
                    "Order in minutes, celebrate for days."),
     "cta_label": "Shop Now", "cta_url": "/offers/tihar",
     "cta_secondary_label": "View Offers", "cta_secondary_url": "/categories",
     "bg_color": "#fffbeb", "text_color": "#b45309"},
    {"title": "Celebrate Dashain", "banner_type": "Promo Strip", "sort_order": 1,
     "heading": "Celebrate Dashain with\nLove and Togetherness",
     "subheading": "Everything you need for a joyful Dashain, delivered to your doorstep.",
     "cta_label": "View Offer", "cta_url": "/offers/dashain",
     "bg_color": "#fef2f2", "text_color": "#b91c1c"},
    {"title": "Make This Tihar Extra Special", "banner_type": "Promo Strip", "sort_order": 2,
     "heading": "Make This Tihar\nExtra Special",
     "subheading": ("Celebrate with festive essentials, premium treats, and "
                    "exclusive offers—all delivered fresh and fast by SaathiMart."),
     "cta_label": "Start Shopping", "cta_url": "/offers/tihar",
     "bg_color": "#fffbeb", "text_color": "#b45309"},
]

def _seed_banners():
    count = 0
    for b in BANNERS:
        if frappe.db.exists("Banner", {"title": b["title"]}):
            continue
        doc = frappe.new_doc("Banner")
        doc.update(b)
        doc.is_active = 1
        doc.insert(ignore_permissions=True)
        count += 1
    print(f"  {count} banners")


# ── Site Config ───────────────────────────────────────────────────────────────

def _seed_site_config():
    doc = frappe.get_single("Site Config")
    doc.site_title = "SaathiMart"
    doc.tagline = "Groceries delivered in minutes across Kathmandu Valley. Built for Nepal."
    doc.legal_name = "SaathiMart Pvt. Ltd."
    # copyright_year intentionally left unset — get_site_config() defaults it
    # to the current year so the footer never goes stale.
    doc.contact_email = "hello@saathimart.np"
    doc.contact_phone = "+977-1-5970001"
    doc.address = "Pulchowk, Lalitpur, Kathmandu Valley, Nepal"
    doc.facebook_url = "https://facebook.com/saathimart"
    doc.instagram_url = "https://instagram.com/saathimart"
    doc.twitter_url = "https://x.com/saathimart"
    doc.newsletter_email_label = "Email Address"
    doc.newsletter_placeholder = "you@example.com"
    doc.newsletter_button_label = "Subscribe"
    doc.meta_title = "SaathiMart — Groceries delivered in minutes"
    doc.meta_description = doc.tagline
    doc.save(ignore_permissions=True)
    print("  site config saved")


# ── Navigation ────────────────────────────────────────────────────────────────
# This storefront's header/footer hardcode their own link structure today
# (routing structure, not content — see components/footer.tsx), so nothing
# currently reads these at runtime. Seeded anyway so Navigation Item /
# get_navigation() work correctly for the admin desk and any future/other
# client that does want a CMS-driven menu.

HEADER_NAV = [
    {"label": "Shop", "url": "/categories", "sort_order": 1},
    {"label": "Offers", "url": "/offers", "sort_order": 2},
    {"label": "About", "url": "/about", "sort_order": 3},
    {"label": "Support", "url": "/support", "sort_order": 4},
]

FOOTER_NAV = [
    {"label": "About Us", "url": "/about", "sort_order": 1},
    {"label": "Blogs", "url": "/blogs", "sort_order": 2},
    {"label": "Careers", "url": "/careers", "sort_order": 3},
    {"label": "Contact Us", "url": "/contact", "sort_order": 4},
    {"label": "Help and Support", "url": "/support", "sort_order": 5},
    {"label": "Track Order", "url": "/orders", "sort_order": 6},
    {"label": "Partner with Us", "url": "/partner", "sort_order": 7},
    {"label": "Become a Rider", "url": "/rider", "sort_order": 8},
    {"label": "Privacy", "url": "/privacy", "sort_order": 9},
    {"label": "Terms", "url": "/terms", "sort_order": 10},
    {"label": "Cookies", "url": "/cookies", "sort_order": 11},
]

def _seed_navigation():
    count = 0
    for location, items in (("Header", HEADER_NAV), ("Footer", FOOTER_NAV)):
        for item in items:
            if frappe.db.exists("Navigation Item", {"label": item["label"], "menu_location": location}):
                continue
            doc = frappe.new_doc("Navigation Item")
            doc.update(item)
            doc.menu_location = location
            doc.is_active = 1
            doc.insert(ignore_permissions=True)
            count += 1
    print(f"  {count} navigation items")


# ── Pages ─────────────────────────────────────────────────────────────────────
# Mirrors saathimart-fe's lib/content/defaults.ts DEFAULT_PAGE_CONTENT
# verbatim, so the CMS starts out agreeing with the frontend's vetted
# fallback copy — admins edit forward from here.

def _p(text):
    return {"kind": "paragraph", "segments": [{"type": "text", "text": text}]}

def _h(text):
    return {"kind": "heading", "text": text}

def _list(*items):
    return {"kind": "list", "items": [[{"type": "text", "text": i}] for i in items]}

def _cta(label, href):
    return {"kind": "cta", "label": label, "href": href}

PAGES = [
    {
        "slug": "about", "page_type": "About",
        "breadcrumb_label": "ABOUT US", "title": "About SaathiMart",
        "subtitle": "Smart shopping starts here — built for the Kathmandu Valley.",
        "sections": [
            _p("SaathiMart started with a simple idea: getting fresh groceries and "
               "everyday essentials shouldn't mean a trip across town. We partner with "
               "local stores across the Kathmandu Valley to bring genuine, quality "
               "products to your door in minutes, not hours."),
            _h("What we stand for"),
            _list(
                "10-minute delivery from stores near you",
                "100% authentic products, sourced from genuine brands",
                "Open 7 AM – 11 PM, every single day",
                "Free delivery on orders over Rs. 500",
            ),
            _h("Built for Nepal"),
            _p("From daily essentials to festival specials, SaathiMart is built around "
               "how Kathmandu Valley households actually shop — quick top-ups, weekly "
               "staples, and everything in between. Whether you're ordering online or "
               "stopping by a SaathiMart store, you get the same authentic products at "
               "the same fair prices."),
        ],
    },
    {
        "slug": "terms", "page_type": "Legal",
        "breadcrumb_label": "TERMS & CONDITIONS", "title": "Terms and Conditions",
        "subtitle": "Last updated 31 July 2026",
        "sections": [
            _p("By placing an order on SaathiMart, you agree to the terms below. "
               "Please read them alongside our Privacy Policy."),
            _h("Orders and delivery"),
            _p("Delivery times shown at checkout are estimates based on stock and "
               "courier availability near you, not a guarantee. Prices and "
               "availability of products may change without notice; if an item "
               "becomes unavailable after ordering, we'll contact you before "
               "substituting or refunding it."),
            _h("Payments"),
            _p("Orders are charged at checkout using your selected payment method. "
               "Discounts and promotional pricing apply only to eligible items and "
               "may be limited per customer or per order."),
            _h("Returns and refunds"),
            _p("If an item arrives damaged, expired, or incorrect, contact us within "
               "24 hours of delivery for a replacement or refund. Fresh produce and "
               "perishables are covered by the same policy — we want every order to "
               "be right."),
            _h("Account use"),
            _p("You're responsible for keeping your account credentials secure. "
               "SaathiMart may suspend accounts used for fraudulent orders or abuse "
               "of promotional offers."),
        ],
    },
    {
        "slug": "privacy", "page_type": "Legal",
        "breadcrumb_label": "PRIVACY POLICY", "title": "Privacy Policy",
        "subtitle": "Last updated 31 July 2026",
        "sections": [
            _p("This policy explains what information SaathiMart collects, how it's "
               "used, and the choices you have. It applies to the SaathiMart website "
               "and app."),
            _h("Information we collect"),
            _list(
                "Account details you provide — name, mobile number, and delivery addresses",
                "Order history, so we can show past orders and speed up reordering",
                "Basic usage data (pages visited, search queries) to improve the "
                "product catalogue and search results",
            ),
            _h("How we use it"),
            _p("We use your information to process orders, coordinate delivery, "
               "provide customer support, and — only with your consent — send order "
               "updates and occasional offers. We don't sell your personal "
               "information to third parties."),
            _h("Your choices"),
            {"kind": "paragraph", "segments": [
                {"type": "text", "text": "You can update your account details or "
                 "request deletion of your data at any time from your account "
                 "settings, or by "},
                {"type": "link", "label": "contacting us", "href": "/contact"},
                {"type": "text", "text": "."},
            ]},
        ],
    },
    {
        "slug": "cookies", "page_type": "Legal",
        "breadcrumb_label": "COOKIE POLICY", "title": "Cookie Policy",
        "subtitle": "Last updated 31 July 2026",
        "sections": [
            _p("SaathiMart uses cookies and similar technologies to keep you signed "
               "in, remember your delivery location and cart, and understand how the "
               "site is used so we can improve it."),
            _h("Types of cookies we use"),
            {"kind": "list", "items": [
                [{"type": "strong", "text": "Essential"},
                 {"type": "text", "text": " — keep you signed in and your cart/wishlist "
                  "in sync; the site doesn't work properly without these"}],
                [{"type": "strong", "text": "Preference"},
                 {"type": "text", "text": " — remember your delivery location and "
                  "display settings"}],
                [{"type": "strong", "text": "Analytics"},
                 {"type": "text", "text": " — help us understand which pages and "
                  "products are most useful, so we can improve them"}],
            ]},
            _h("Managing cookies"),
            _p("Most browsers let you block or delete cookies in their settings. "
               "Blocking essential cookies may prevent sign-in and cart features "
               "from working correctly."),
        ],
    },
    {
        "slug": "careers", "page_type": "Custom",
        "breadcrumb_label": "CAREERS", "title": "Careers at SaathiMart",
        "subtitle": "Help us bring smart shopping to every household in the Kathmandu Valley.",
        "sections": [
            _p("We're a small, fast-moving team building the quickest way to shop "
               "for groceries and essentials in Nepal. We're always looking for "
               "people who care about doing things properly — from warehouse "
               "operations to engineering to customer support."),
            _h("Open roles"),
            _p("We don't have a public roles board yet — send us a note about what "
               "you'd like to do and we'll get back to you."),
            _cta("Get in Touch", "/contact"),
        ],
    },
    {
        "slug": "partner", "page_type": "Custom",
        "breadcrumb_label": "PARTNER WITH US", "title": "Partner with SaathiMart",
        "subtitle": "List your store on SaathiMart and reach shoppers across the Kathmandu Valley.",
        "sections": [
            _p("SaathiMart partners with local grocery and essentials stores to "
               "offer 10-minute delivery without either of us building a warehouse "
               "network from scratch. You keep running your store; we bring the "
               "orders and the riders."),
            _h("Why partner with us"),
            _list(
                "Reach customers already shopping near your store",
                "No upfront fees to get listed",
                "We handle delivery — you handle what you're good at",
            ),
            _cta("Apply to Partner", "/contact"),
        ],
    },
    {
        "slug": "rider", "page_type": "Custom",
        "breadcrumb_label": "BECOME A RIDER", "title": "Become a SaathiMart Rider",
        "subtitle": "Flexible hours, fair pay, deliveries close to home.",
        "sections": [
            _p("SaathiMart riders are the reason 10-minute delivery works. Ride when "
               "it suits you, pick up orders from stores near you, and get paid "
               "weekly."),
            _h("What you need"),
            _list(
                "A valid driving license and your own bike or scooter",
                "A smartphone for the rider app",
                "Availability in Kathmandu, Lalitpur, or Bhaktapur",
            ),
            _cta("Apply to Ride", "/contact"),
        ],
    },
]

def _seed_pages():
    count = 0
    for p in PAGES:
        if frappe.db.exists("Site Page", {"slug": p["slug"]}):
            continue
        doc = frappe.new_doc("Site Page")
        doc.slug = p["slug"]
        doc.title = p["title"]
        doc.page_type = p["page_type"]
        doc.breadcrumb_label = p["breadcrumb_label"]
        doc.subtitle = p["subtitle"]
        doc.meta_title = p["title"]
        doc.meta_description = p["subtitle"]
        doc.sections = json.dumps(p["sections"])
        doc.status = "Published"
        doc.published_at = frappe.utils.now_datetime()
        doc.insert(ignore_permissions=True)
        count += 1
    print(f"  {count} pages")


# ── Blog Posts ────────────────────────────────────────────────────────────────

BLOG_POSTS = [
    {"title": "5 Ways to Cut Your Grocery Bill Without Cutting Corners",
     "slug": "cut-grocery-bill-without-corners", "author": "SaathiMart Team",
     "category": "Tips & Tricks", "tags": "budgeting,groceries,savings",
     "excerpt": "Smart substitutions, bulk buys, and timing your order right — "
                "small habits that add up to real savings every month.",
     "content": "<p>Groceries are one of the easiest budgets to trim once you know "
                "where to look. Buy staples like rice and lentils in bulk, watch for "
                "our weekly Wholesale pricing on produce, and keep an eye on the "
                "app's daily deals — they rotate often.</p>"},
    {"title": "What's in Season This Dashain",
     "slug": "whats-in-season-this-dashain", "author": "SaathiMart Team",
     "category": "Seasonal", "tags": "dashain,festival,produce",
     "excerpt": "The freshest produce and festival staples to look for this "
                "Dashain, straight from local farms around the Valley.",
     "content": "<p>Dashain means fuller kitchens and bigger gatherings. Stock up "
                "on fresh mutton, seasonal vegetables, and the sweets that make the "
                "festival what it is — all available for 10-minute delivery.</p>"},
    {"title": "Inside a SaathiMart 10-Minute Delivery",
     "slug": "inside-a-10-minute-delivery", "author": "SaathiMart Team",
     "category": "Behind the Scenes", "tags": "delivery,riders,logistics",
     "excerpt": "From the moment you tap 'Place Order' to the knock on your door — "
                "here's what actually happens in those 10 minutes.",
     "content": "<p>Your order is routed to the nearest partner store with stock, "
                "packed by store staff, and picked up by a rider already nearby. "
                "No warehouses, no long hauls — just short hops from a store you "
                "could probably walk to.</p>"},
]

def _seed_blog_posts():
    count = 0
    for b in BLOG_POSTS:
        if frappe.db.exists("Blog Post", {"slug": b["slug"]}):
            continue
        doc = frappe.new_doc("Blog Post")
        doc.update(b)
        doc.status = "Published"
        doc.published_at = frappe.utils.now_datetime()
        doc.insert(ignore_permissions=True)
        count += 1
    print(f"  {count} blog posts")
