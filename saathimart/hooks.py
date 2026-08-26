app_name = "saathimart"
app_title = "SaathiMart"
app_publisher = "Trevo Cloud Nepal"
app_description = "Central commerce hub — Blinkit-style, Frappe-native, no ERPNext"
app_email = "dev@trevo.com.np"
app_license = "mit"
app_version = "0.1.0"

required_apps = ["frappe"]

# ── Error envelope ────────────────────────────────────────────────────────────
# Rewrites framework-level failures on /api/method/saathimart.* routes into
# the canonical {"ok": False, "error", "error_code"} body — see api/responses.py.
after_request = [
    "saathimart.api.responses.normalize_error_response",
]

# ── Desk tile ─────────────────────────────────────────────────────────────────
add_to_apps_screen = [
    {
        "name": "saathimart",
        "logo": "/assets/saathimart/images/logo.svg",
        "title": "SaathiMart",
        "route": "/app/saathimart",
        "has_permission": "saathimart.api.auth.has_app_permission",
    }
]

# NOTE: doctype_js is NOT needed here.
# Frappe automatically loads <doctype>/<doctype>.js for every custom DocType
# in this app. doctype_js in hooks.py is only for injecting JS into
# *other apps'* DocTypes (e.g. ERPNext's Sales Order).

# ── Document events ───────────────────────────────────────────────────────────
doc_events = {
    "Order": {
        "after_insert": "saathimart.events.publisher.on_order_created",
        "on_update":    "saathimart.events.publisher.on_order_updated",
    },
    "Product": {
        "after_insert": "saathimart.events.publisher.on_product_created",
        "on_update":    "saathimart.events.publisher.on_product_updated",
        "on_trash":     "saathimart.events.publisher.on_product_deleted",
    },
    "Review": {
        "on_update": "saathimart.api.reviews._update_product_rating",
    },
    "Vendor Listing": {
        "after_insert": "saathimart.events.publisher.on_vendor_listing_changed",
        "on_update":    "saathimart.events.publisher.on_vendor_listing_changed",
        "on_trash":     "saathimart.events.publisher.on_vendor_listing_changed",
    },
    "Vendor": {
        "on_update": "saathimart.events.publisher.on_vendor_updated",
    },
    "Site Config": {
        "on_update": "saathimart.api.cms._bust_site_config_cache",
    },
    "Navigation Item": {
        "on_update": "saathimart.api.cms._bust_navigation_cache",
    },
    "Banner": {
        "on_update": "saathimart.api.cms._bust_banner_cache",
    },
    "Site Page": {
        "on_update": "saathimart.api.cms._bust_page_cache",
    },
    "Blog Post": {
        "on_update": "saathimart.api.cms._bust_blog_cache",
    },
    "FAQ Category": {
        "on_update": "saathimart.api.cms._bust_faq_category_cache",
    },
    "FAQ Item": {
        "on_update": "saathimart.api.cms._bust_faq_item_cache",
    },
    "Offer": {
        "on_update": "saathimart.api.cms._bust_offer_cache_on_update",
    },
    "Popular Location": {
        "on_update": "saathimart.api.cms._bust_location_cache_on_update",
    },
    "Hero Slide": {
        "on_update": "saathimart.api.cms._bust_home_content_cache",
    },
    "Seasonal Banner": {
        "on_update": "saathimart.api.cms._bust_home_content_cache",
    },
    "Trust Badge": {
        "on_update": "saathimart.api.cms._bust_home_content_cache",
    },
    "Product Rail Heading": {
        "on_update": "saathimart.api.cms._bust_home_content_cache",
    },
    "Homepage Settings": {
        "on_update": "saathimart.api.cms._bust_home_content_cache",
    },
    "Website Content": {
        "on_update": "saathimart.api.cms._bust_content_cache",
    },
}

# ── Scheduled tasks ───────────────────────────────────────────────────────────
scheduler_events = {
    "daily": [
        "saathimart.api.loyalty.expire_old_points",
        "saathimart.api.membership.expire_memberships",
        "saathimart.api.archival.archive_old_data",
        # Purge expired OTP rows (ported from saathi_middleware)
        "saathimart.api.auth_full.cleanup_expired_verifications",
        # Digest of dead/stuck webhook events — emailed to System Managers
        "saathimart.events.monitoring.daily_sync_health_report",
    ],
    "hourly": [
        "saathimart.api.cart.expire_abandoned_carts",
        "saathimart.events.publisher.flush_failed_webhooks",
    ],
    "cron": {
        "*/2 * * * *": [
            "saathimart.events.publisher.drain_event_queue",
        ],
        "*/10 * * * *": [
            "saathimart.api.payments.poll_pending_esewa_orders",
        ],
        "*/5 * * * *": [
            "saathimart.api.stock.check_negative_vendor_stock",
        ],
        "0 * * * *": [
            "saathimart.api.orders.expire_pending_payment_orders",
        ],
    },
}

# ── Fixtures ──────────────────────────────────────────────────────────
fixtures = [
    {"dt": "Role", "filters": [["name", "in", [
        "SM Admin", "SM Vendor", "SM Delivery", "SM Customer", "Website Manager",
    ]]]},
    {"dt": "Settings"},
    {"dt": "Site Config"},
    {"dt": "Homepage Settings"},
    {"dt": "Pending Verification"},
    {"dt": "Hero Slide"},
    {"dt": "Seasonal Banner"},
    {"dt": "Trust Badge"},
    {"dt": "Product Rail Heading"},
    {"dt": "Website Content"},
]

# ── Permissions ───────────────────────────────────────────────────────────────
has_permission = {
    "Cart":    "saathimart.api.auth.has_cart_permission",
    "Order":   "saathimart.api.auth.has_order_permission",
    "Address": "saathimart.api.auth.has_address_permission",
}

# ── Boot info ─────────────────────────────────────────────────────────
extend_bootinfo = "saathimart.api.auth.extend_bootinfo"