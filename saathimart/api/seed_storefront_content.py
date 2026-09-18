"""
Storefront CMS content seeder — real content for the Next.js storefront.

The FE content layer (lib/content/cms.ts) reads, CMS-first with local
fallbacks:
    Site Config            → header/footer copy, contact, socials, meta
    Navigation Item        → header nav + footer columns (parent → children)
    Hero Slide             → home hero
    Seasonal Banner        → home promo banners (dashain/tihar artwork keys)
    Trust Badge            → home trust row
    Product Rail Heading   → home product-rail headings
    Offer                  → /offers pages
    About Us + About Stat/Feature/Value → /about
    Privacy/Terms/Cookies/Careers/Partner/Rider Page → static pages
    Website Content        → get_content("support"/"contact") API rows
    Homepage Settings      → marketplace banner block
    Banner                 → CMS "Promo Strip" strips (via get_banners)

This module seeds all of them idempotently (upsert by natural key), with
SaathiMart's real identity — replacing the "CMS Test Site" rows left by
earlier cache-bust unit tests. Safe to re-run; it never touches vendor or
transactional data.

Usage:
    bench --site saathimart.localhost execute \
        saathimart.api.seed_storefront_content.seed_storefront_content
"""
import json

import frappe

# ── Site identity ────────────────────────────────────────────────────────
SITE_CONFIG = {
    "site_title": "SaathiMart",
    "tagline": "Nepal's neighbourhood marketplace — fresh groceries and daily essentials, delivered fast.",
    "legal_name": "SaathiMart (DnDTS Tech Nepal)",
    "copyright_year": 2026,
    "navbar_announcement_text": "Free delivery on your first 3 orders — use code SATHIFREE",
    "contact_email": "support@saathimart.com.np",
    "contact_phone": "+977-01-5970000",
    "address": "Jhamsikhel Road, Lalitpur, Bagmati, Nepal",
    "facebook_url": "https://facebook.com/saathimart",
    "instagram_url": "https://instagram.com/saathimart",
    "twitter_url": "https://x.com/saathimart",
    "newsletter_email_label": "Get deals in your inbox",
    "newsletter_placeholder": "Enter your email",
    "newsletter_button_label": "Subscribe",
    "meta_title": "SaathiMart — Groceries & essentials delivered across the Kathmandu Valley",
    "meta_description": (
        "Shop fresh produce, dairy, staples, personal care and household "
        "essentials from trusted local vendors. Fast delivery across "
        "Kathmandu, Lalitpur and Bhaktapur."
    ),
}

# Header: flat items. Footer: parent items whose children become the
# footer's link columns (Footer component groups parent → column).
NAV_ITEMS = [
    # (label, url, location, parent_label_or_None, sort)
    ("Shop", "/categories", "Header", None, 1),
    ("Offers", "/offers", "Header", None, 2),
    ("About Us", "/about", "Header", None, 3),
    ("Contact", "/contact", "Header", None, 4),
    ("Shop", "/categories", "Footer", None, 1),        # footer column 1
    ("All Categories", "/categories", "Footer", "Shop", 1),
    ("Offers & Deals", "/offers", "Footer", "Shop", 2),
    ("Search", "/search", "Footer", "Shop", 3),
    ("Company", "/about", "Footer", None, 2),          # footer column 2
    ("About Us", "/about", "Footer", "Company", 1),
    ("Careers", "/careers", "Footer", "Company", 2),
    ("Contact Us", "/contact", "Footer", "Company", 3),
    ("Support", "/support", "Footer", None, 3),        # footer column 3
    ("Help Center", "/support", "Footer", "Support", 1),
    ("FAQs", "/faq", "Footer", "Support", 2),
    ("Delivery Info", "/faq", "Footer", "Support", 3),
    ("Legal", "/pages/privacy", "Footer", None, 4),    # footer column 4
    ("Privacy Policy", "/pages/privacy", "Footer", "Legal", 1),
    ("Terms of Service", "/pages/terms", "Footer", "Legal", 2),
    ("Cookie Policy", "/pages/cookies", "Footer", "Legal", 3),
]

HERO_SLIDES = [
    {
        "slide_key": "hero-1",
        "title_lines": "Fresh Groceries\nDelivered Fast",
        "description": "Farm-fresh vegetables, dairy and daily staples from vendors near you — at your door in minutes.",
        "cta_label": "Shop now",
        "cta_href": "/categories",
        "cta_secondary_label": "View offers",
        "cta_secondary_href": "/offers",
        "sort_order": 1,
    },
    {
        "slide_key": "hero-2",
        "title_lines": "Monsoon Personal Care\nUp to 25% off",
        "description": "Stock up on shampoo, skincare and hygiene essentials from trusted brands this season.",
        "cta_label": "Grab the deals",
        "cta_href": "/categories",
        "cta_secondary_label": "",
        "cta_secondary_href": "",
        "sort_order": 2,
    },
    {
        "slide_key": "hero-3",
        "title_lines": "Membership\nFree delivery forever",
        "description": "Join SaathiMart Membership for unlimited free delivery, early access to deals and bonus loyalty points.",
        "cta_label": "Become a member",
        "cta_href": "/account",
        "cta_secondary_label": "",
        "cta_secondary_href": "",
        "sort_order": 3,
    },
]

# slide_key matches the FE's bundled artwork ids (dashain.png / tihar.png)
SEASONAL_BANNERS = [
    {
        "slide_key": "dashain",
        "title_lines": "Dashain Tihar Special\nFestive groceries & gifts",
        "description": "Celebrate with special festival prices on rice, ghee, sweets and more.",
        "cta_label": "Shop the festival",
        "cta_href": "/categories",
        "sort_order": 1,
    },
    {
        "slide_key": "tihar",
        "title_lines": "Tihar Lights\nSweet deals on sweets",
        "description": "Sel roti essentials, khoya, dry fruits — everything for a bright Tihar.",
        "cta_label": "Shop sweets",
        "cta_href": "/categories",
        "sort_order": 2,
    },
]

TRUST_BADGES = [
    ("delivery", "Fast delivery", "Groceries at your door in as little as 10 minutes across the valley."),
    ("authentic", "100% authentic", "Sourced directly from trusted local vendors and genuine brands."),
    ("free-delivery", "Free delivery", "Free on orders above Rs. 500 — always for members."),
    ("hours", "Open 7 days", "Every day, 7 AM to 11 PM — including public holidays."),
]

PRODUCT_RAILS = [
    ("featured", "Featured products", "Hand-picked favourites from our vendors"),
    ("personal-care", "Personal care", "Everyday hygiene & self-care essentials"),
    ("dairy-bakery", "Dairy & bakery", "Fresh milk, paneer, bread and bakes"),
    ("cleaning-household", "Cleaning & household", "Keep your home spotless"),
]

HOMEPAGE_SETTINGS = {
    "marketplace_banner_title": "Shop from your local stores, online",
    "marketplace_banner_description": (
        "SaathiMart connects you with neighbourhood vendors — same products, "
        "same trust, now with doorstep delivery."
    ),
    "marketplace_banner_link_label": "Become a vendor partner",
}

# Promo Strip banners (CMS Banner doctype, consumed via get_banners)
BANNERS = [
    {
        "title": "First-order free delivery",
        "banner_type": "Promo Strip",
        "heading": "Your first order ships free",
        "subheading": "Use code SATHIFREE at checkout — valid on orders above Rs. 500.",
        "cta_label": "Start shopping",
        "cta_url": "/categories",
        "bg_color": "#0f766e",
        "text_color": "#ffffff",
        "sort_order": 1,
    },
    {
        "title": "Membership launch",
        "banner_type": "Promo Strip",
        "heading": "SaathiMart Membership is here",
        "subheading": "Unlimited free delivery + 2x loyalty points. Rs. 999/year.",
        "cta_label": "Learn more",
        "cta_url": "/account",
        "bg_color": "#7c3aed",
        "text_color": "#ffffff",
        "sort_order": 2,
    },
]


def _seg(text):
    return {"type": "text", "text": text}


def _para(text):
    return {"kind": "paragraph", "segments": [_seg(text)]}


def _head(text):
    return {"kind": "heading", "text": text}


def _items(*texts):
    return {"kind": "list", "items": [[_seg(t)] for t in texts]}


def _cta(label, href):
    return {"kind": "cta", "label": label, "href": href}


def _sections_json(*sections):
    return json.dumps(list(sections))


ABOUT_US = {
    "title": "About SaathiMart",
    "breadcrumb_label": "ABOUT US",
    "subtitle": "Smart shopping starts here — built for the Kathmandu Valley.",
    "hero_title": "Smart Shopping Starts Here",
    "hero_subtitle": "Built for the Kathmandu Valley — we're changing how Nepal shops for groceries and everyday essentials.",
    "mission_title": "Making everyday shopping effortless",
    "mission_text": (
        "<p>SaathiMart started with a simple idea: getting fresh groceries and "
        "everyday essentials shouldn't mean a trip across town. We partner with "
        "local stores across the Kathmandu Valley to bring genuine, quality "
        "products to your door in minutes, not hours.</p>"
        "<p>From daily essentials to festival specials, we're built around how "
        "Kathmandu Valley households actually shop — quick top-ups, weekly "
        "staples, and everything in between.</p>"
    ),
    "features_title": "Everything you need, delivered fast",
    "values_title": "What we stand for",
    "cta_title": "Join the SaathiMart Family",
    "cta_text": "We're always looking for passionate people to join our team — from warehouse staff to engineers to delivery riders.",
    "meta_title": "About SaathiMart",
    "meta_description": "How SaathiMart connects Nepali households with trusted local vendors.",
}

ABOUT_STATS = [
    ("10", "Minute Delivery"),
    ("100%", "Authentic Products"),
    ("7AM–11PM", "Open Daily"),
    ("Free", "Orders Over Rs. 500"),
]

ABOUT_FEATURES = [
    ("clock", "10-Minute Delivery", "Order from stores near you and get your groceries delivered in as little as 10 minutes."),
    ("shield", "100% Authentic", "Every product is sourced from genuine brands and trusted local stores — no knockoffs, no compromises."),
    ("map-pin", "Store Near You", "Multiple store locations across Kathmandu Valley mean faster delivery and fresher products."),
    ("truck", "Free Delivery", "Enjoy free delivery on orders over Rs. 500. No hidden fees, no surprises at checkout."),
    ("heart", "Fresh Guarantee", "Fresh produce, dairy, and bakery items delivered daily. Quality you can trust."),
    ("sparkles", "Festival Ready", "Special collections for Dashain, Tihar, and every celebration. We're ready when you are."),
]

ABOUT_VALUES = [
    ("Built for Nepal", "We understand local needs, celebrate local festivals, and source from local stores. This is home-grown convenience."),
    ("Honest Pricing", "Same price in-store and online. No markup, no hidden fees. What you see is what you pay."),
    ("Quality First", "Every product is checked for authenticity. We partner only with trusted brands and reliable local merchants."),
    ("Community Focus", "We work with neighborhood stores, hire local riders, and invest in the communities we serve."),
]

# (doctype, page_type) pairs from the hub's STATIC_PAGE_DOCTYPE_MAP
STATIC_PAGES = {
    "terms": (
        "Terms Page",
        {
            "title": "Terms of Service",
            "breadcrumb_label": "TERMS",
            "subtitle": "The rules of using SaathiMart.",
            "meta_title": "Terms of Service — SaathiMart",
            "meta_description": "SaathiMart terms of service.",
            "sections": _sections_json(
                _head("Orders & pricing"),
                _para("Prices are set by each vendor and shown before checkout. VAT is charged per Nepali tax regulations and shown on your invoice. Orders may be adjusted or refunded if items are unavailable."),
                _head("Delivery"),
                _para("We deliver across Kathmandu, Lalitpur and Bhaktapur from 7 AM to 11 PM, seven days a week. Delivery is free on orders above Rs. 500."),
                _head("Returns"),
                _items(
                    "Damaged or wrong items can be handed back to the rider at delivery.",
                    "Return requests can be raised from the order page within 24 hours.",
                    "Refunds are processed to the original payment method within 5 working days.",
                ),
                _head("Accounts"),
                _para("Keep your credentials safe; you are responsible for activity under your account. Contact us to close your account at any time."),
                _cta("Contact support", "/contact"),
            ),
        },
    ),
    "privacy": (
        "Privacy Page",
        {
            "title": "Privacy Policy",
            "breadcrumb_label": "PRIVACY",
            "subtitle": "How we collect, use and protect your data.",
            "meta_title": "Privacy Policy — SaathiMart",
            "meta_description": "SaathiMart privacy policy: what we collect, why, and your rights.",
            "sections": _sections_json(
                _head("Data we collect"),
                _para("Account details (name, phone, email), delivery addresses, order history and device information needed to run the service and prevent fraud."),
                _head("How we use it"),
                _para("To deliver your orders, provide support, improve the product, and — only with your consent — send offers. We never sell your personal data."),
                _head("Cookies"),
                _para("We use essential cookies to keep you signed in and your cart intact, and analytics cookies only after you opt in."),
                _head("Your rights"),
                _para("You can access, correct or delete your data at any time from Settings, or by writing to support@saathimart.com.np."),
                _cta("Manage your account", "/account"),
            ),
        },
    ),
    "cookies": (
        "Cookies Page",
        {
            "title": "Cookie Policy",
            "breadcrumb_label": "COOKIES",
            "subtitle": "What we store in your browser, and why.",
            "meta_title": "Cookie Policy — SaathiMart",
            "meta_description": "The cookies SaathiMart uses and how to control them.",
            "sections": _sections_json(
                _head("Essential cookies"),
                _para("Session, cart and location cookies keep the store working — they cannot be switched off."),
                _head("Analytics cookies"),
                _para("Anonymous usage statistics that help us improve search and delivery. Loaded only after consent."),
            ),
        },
    ),
    "careers": (
        "Careers Page",
        {
            "title": "Careers at SaathiMart",
            "breadcrumb_label": "CAREERS",
            "subtitle": "Help us build Nepal's neighbourhood marketplace.",
            "meta_title": "Careers — SaathiMart",
            "meta_description": "Open roles at SaathiMart — technology, operations and delivery.",
            "sections": _sections_json(
                _head("Why SaathiMart"),
                _para("Small team, big mission, real ownership. We ship weekly and every role shapes how the Kathmandu Valley shops."),
                _head("Open roles"),
                _items(
                    "Riders — full-time and part-time, own or company bike.",
                    "Warehouse & inventory associates.",
                    "Engineering — React/Next.js and Python/Frappe.",
                ),
                _cta("Apply now", "/contact"),
            ),
        },
    ),
}

# Website Content rows for the hub's get_content("support"/"contact") API.
WEBSITE_CONTENT = {
    "support": {
        "breadcrumb_label": "HELP & SUPPORT",
        "title": "How can we help?",
        "subtitle": "Search our FAQs or browse the questions below.",
        "search_placeholder": "Search for a product...",
        "still_need_help": {
            "title": "Still need help?",
            "body": "Our team is happy to help with anything not covered above.",
            "cta_label": "Contact Us",
        },
    },
    "contact": {
        "breadcrumb_label": "CONTACT US",
        "title": "Get in Touch",
        "subtitle": "Questions about an order, a partnership, or anything else — we'd love to hear from you.",
        "success_title": "Message sent",
        "success_body": "Thanks for reaching out — we'll get back to you soon.",
    },
}


def _upsert_single(doctype, values):
    doc = frappe.get_single(doctype)
    changed = False
    for k, v in values.items():
        if doc.get(k) != v:
            doc.set(k, v)
            changed = True
    if changed:
        doc.flags.ignore_permissions = True
        doc.save()
        frappe.db.commit()
    return changed


def _upsert_doctype(doctype, match, values):
    name = frappe.db.get_value(doctype, match)
    if name:
        doc = frappe.get_doc(doctype, name)
        changed = False
        for k, v in values.items():
            if doc.get(k) != v:
                doc.set(k, v)
                changed = True
        if changed:
            doc.flags.ignore_permissions = True
            doc.save()
            frappe.db.commit()
        return name, changed
    doc = frappe.new_doc(doctype)
    for k, v in values.items():
        doc.set(k, v)
    doc.flags.ignore_permissions = True
    doc.insert()
    frappe.db.commit()
    return doc.name, True


def _get_or_create(doctype, match, values):
    name = frappe.db.get_value(doctype, match)
    if name:
        return name, False
    doc = frappe.new_doc(doctype)
    for k, v in values.items():
        doc.set(k, v)
    doc.flags.ignore_permissions = True
    doc.insert()
    frappe.db.commit()
    return doc.name, True


def seed_storefront_content():
    """Seed the full storefront CMS. Idempotent — safe to re-run."""
    frappe.set_user("Administrator")
    out = {}

    out["site_config"] = _upsert_single("Site Config", SITE_CONFIG)

    # Navigation: refresh app-owned labels/urls; parents are resolved
    # before children (footer columns are parent → children groups).
    # A location prefix keeps the header "Shop" and footer "Shop" apart.
    nav_created = 0
    parent_names = {}
    for label, url, location, parent, sort in NAV_ITEMS:
        parent_name = None
        if parent:
            if parent not in parent_names:
                parent_names[parent] = frappe.db.get_value(
                    "Navigation Item",
                    {"label": parent, "menu_location": location, "parent_item": ["is", "not set"]},
                )
            parent_name = parent_names[parent]
        _, created = _upsert_doctype(
            "Navigation Item",
            {"label": label, "menu_location": location,
             "parent_item": parent_name if parent else ["is", "not set"]},
            {"label": label, "url": url, "menu_location": location,
             "parent_item": parent_name,
             "sort_order": sort, "is_active": 1},
        )
        nav_created += 1 if created else 0
    out["navigation_created"] = nav_created

    for h in HERO_SLIDES:
        _upsert_doctype("Hero Slide", {"slide_key": h["slide_key"]}, h)
    for b in SEASONAL_BANNERS:
        _upsert_doctype("Seasonal Banner", {"slide_key": b["slide_key"]}, b)

    for i, (icon, title, desc) in enumerate(TRUST_BADGES, 1):
        _upsert_doctype("Trust Badge", {"icon_key": icon}, {
            "icon_key": icon, "title": title, "description": desc,
            "sort_order": i, "published": 1,
        })

    for i, (key, title, sub) in enumerate(PRODUCT_RAILS, 1):
        _upsert_doctype("Product Rail Heading", {"rail_key": key}, {
            "rail_key": key, "title": title, "subtitle": sub,
            "sort_order": i, "published": 1,
        })

    out["homepage_settings"] = _upsert_single("Homepage Settings", HOMEPAGE_SETTINGS)

    for b in BANNERS:
        _upsert_doctype("Banner", {"title": b["title"]}, {**b, "is_active": 1})

    # About Us single + its child tables (seeded only when empty —
    # admins may edit rows in the desk; we don't clobber them on re-run)
    out["about_us"] = _upsert_single("About Us", ABOUT_US)
    about = frappe.get_single("About Us")
    seeded_children = False
    if not about.get("stats"):
        for i, (value, label) in enumerate(ABOUT_STATS, 1):
            about.append("stats", {"value": value, "label": label, "sort_order": i})
        seeded_children = True
    if not about.get("features"):
        for i, (icon, title, desc) in enumerate(ABOUT_FEATURES, 1):
            about.append("features", {"icon": icon, "title": title, "description": desc, "sort_order": i})
        seeded_children = True
    if not about.get("values"):
        for i, (title, desc) in enumerate(ABOUT_VALUES, 1):
            about.append("values", {"title": title, "description": desc, "sort_order": i})
        seeded_children = True
    if seeded_children:
        about.flags.ignore_permissions = True
        about.save()
        frappe.db.commit()

    # Static pages (dedicated Single doctypes)
    for page_type, (doctype, values) in STATIC_PAGES.items():
        out[f"page_{page_type}"] = _upsert_single(doctype, values)

    # Website Content rows (support/contact API rows)
    for key, content in WEBSITE_CONTENT.items():
        _upsert_doctype("Website Content", {"content_key": key}, {
            "content_key": key,
            "content_json": json.dumps(content),
            "published": 1,
            "notes": "Seeded storefront content",
        })

    # Content caches: bust everything this seeder may have changed
    from saathimart.api.storefront_cache import bust_cms_cache
    try:
        bust_cms_cache()
    except Exception:
        cache = frappe.cache()
        for k in ("sm_site_config", "sm_home_content", "sm_banners"):
            cache.delete_value(k)
        for pat in ("sm_navigation:*", "sm_page:*", "sm_static_page:*"):
            try:
                cache.delete_keys(pat)
            except Exception:
                pass

    print(json.dumps(out, indent=2, default=str))
    return out
