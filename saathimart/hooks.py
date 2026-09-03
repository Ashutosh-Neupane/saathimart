app_name = "saathimart"
app_title = "SaathiMart"
app_publisher = "Trevo Cloud Nepal"
app_description = "Central commerce hub — Blinkit-style, Frappe-native, no ERPNext"
app_email = "dev@trevo.com.np"
app_license = "mit"
app_version = "0.1.0"

required_apps = ["frappe"]

# ── Request tracking & error envelope ──────────────────────────────────────────
# before_request: Add X-Request-ID for log correlation
# after_request: Add rate limit headers and normalize errors
before_request = [
    "saathimart.api.request_tracking.add_request_id",
]
after_request = [
    "saathimart.api.request_tracking.add_rate_limit_headers",
    "saathimart.api.responses.normalize_error_response",
    "saathimart.api.cache_headers.apply_cache_headers",
]

# ── Performance: after_migrate hooks (cache warming + index verification) ────
after_migrate = [
    "saathimart.api.cache_warming.warm_cache",
    "saathimart.api.indexes.add_performance_indexes",
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
        "after_insert": [
            "saathimart.events.publisher.on_order_created",
            "saathimart.api.order_events.on_order_created",
            "saathimart.api.audit.log_order_update",
        ],
        "on_update": [
            "saathimart.events.publisher.on_order_updated",
            "saathimart.api.order_events.on_order_paid",
            "saathimart.api.audit.log_order_update",
        ],
    },
    "Product": {
        "after_insert": [
            "saathimart.events.publisher.on_product_created",
            "saathimart.api.audit.log_product_update",
        ],
        "on_update": [
            "saathimart.events.publisher.on_product_updated",
            "saathimart.api.audit.log_product_update",
        ],
        "on_trash":     "saathimart.events.publisher.on_product_deleted",
    },
    "Review": {
        "on_update": "saathimart.api.reviews._update_product_rating",
    },
    "SM Product Review": {
        "on_update": "saathimart.doctype.sm_product_review.sm_product_review._recompute_product_rating",
        "on_trash": "saathimart.doctype.sm_product_review.sm_product_review._recompute_product_rating",
    },
    "SM Audit Log": {
        "after_insert": "saathimart.api.audit.log_audit_entry",
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
    "About Us": {
        "on_update": "saathimart.api.cms._bust_static_page_cache",
    },
    "Terms Page": {
        "on_update": "saathimart.api.cms._bust_static_page_cache",
    },
    "Privacy Page": {
        "on_update": "saathimart.api.cms._bust_static_page_cache",
    },
    "Cookies Page": {
        "on_update": "saathimart.api.cms._bust_static_page_cache",
    },
    "Careers Page": {
        "on_update": "saathimart.api.cms._bust_static_page_cache",
    },
    "Partner Page": {
        "on_update": "saathimart.api.cms._bust_static_page_cache",
    },
    "Rider Page": {
        "on_update": "saathimart.api.cms._bust_static_page_cache",
    },
}

# ── Scheduled tasks ───────────────────────────────────────────────────────────
scheduler_events = {
    "daily": [
        "saathimart.api.loyalty.expire_old_points",
        "saathimart.api.membership.expire_memberships",
        "saathimart.api.archival.archive_old_data",
        # Purge expired OTP rows from the verification store.
        "saathimart.api.auth_full.cleanup_expired_verifications",
        # Digest of dead/stuck webhook events — emailed to System Managers
        "saathimart.events.monitoring.daily_sync_health_report",
        # Dead letter auto-recovery: retry recent dead events
        "saathimart.api.dead_letter.retry_dead_letters",
        # Dead letter alert when threshold exceeded
        "saathimart.api.dead_letter.dead_letter_alert",
        # Alert (never auto-rotates) when a vendor's webhook secret is overdue
        "saathimart.api.secret_rotation.check_stale_secrets",
        # Push notification: clean up stale device tokens (90-day inactivity)
        "saathimart.api.push_notifications.cleanup_stale_tokens",
        # SSE: clean up stale connections (10-minute timeout)
        "saathimart.api.sse.cleanup_stale_connections",
    ],
    "hourly": [
        "saathimart.api.cart.expire_abandoned_carts",
        "saathimart.events.publisher.flush_failed_webhooks",
        "saathimart.api.reconciliation.reconcile_stock_hourly",
        # Stock snapshot sync: send full stock state to each vendor
        # (reconciliation checks individual products; snapshot sends everything)
        "saathimart.api.stock_snapshot.sync_all_stock_snapshots",
        # Keep Vendor Listing's cached qty fields from drifting away from
        # the authoritative Vendor Stock table.
        "saathimart.api.stock.sync_vendor_listing_stock",
    ],
    "weekly": [
        # Archive old dead-letter events older than 30 days
        "saathimart.api.dead_letter.archive_old_events",
    ],
    "cron": {
        "*/2 * * * *": [
            "saathimart.events.publisher.drain_event_queue",
            # SSE: clean up stale connections every 2 minutes
            "saathimart.api.sse.cleanup_stale_connections",
        ],
        "*/10 * * * *": [
            "saathimart.api.payments.poll_pending_esewa_orders",
        ],
        "*/5 * * * *": [
            "saathimart.api.stock.check_negative_vendor_stock",
        ],
        "0 * * * *": [
            "saathimart.api.orders.expire_pending_payment_orders",
            "saathimart.api.orders.retry_failed_order_syncs",
        ],
    },
}

# ── Fixtures ──────────────────────────────────────────────────────────
# Exported on every `bench migrate` / `bench export-fixtures` so that
# configuration data (roles, settings, CMS content) is portable across
# environments. Only doctypes that hold *configuration* or *content*
# are listed — transactional data (Cart, Order, Vendor Stock, etc.) is
# environment-specific and must not be fixtures-exported.
fixtures = [
    # ── Roles & Permissions ──
    {"dt": "Role", "filters": [["name", "in", [
        "SM Admin", "SM Vendor", "SM Delivery", "SM Customer", "Website Manager",
    ]]]},
    # ── Core Settings ──
    {"dt": "Settings"},
    {"dt": "Site Config"},
    {"dt": "Homepage Settings"},
    # ── Authentication ──
    {"dt": "Pending Verification"},
    # ── CMS Content ──
    {"dt": "Hero Slide"},
    {"dt": "Seasonal Banner"},
    {"dt": "Trust Badge"},
    {"dt": "Product Rail Heading"},
    {"dt": "Website Content"},
    {"dt": "SM Product Review"},
    {"dt": "SM Search Term"},
    {"dt": "SM Audit Log"},
    {"dt": "SM Feature Flag"},
    # ── Static Pages ──
    {"dt": "About Us"},
    {"dt": "Terms Page"},
    {"dt": "Privacy Page"},
    {"dt": "Cookies Page"},
    {"dt": "Careers Page"},
    {"dt": "Partner Page"},
    {"dt": "Rider Page"},
    # ── Catalogue Master Data ──
    {"dt": "Category"},
    {"dt": "Brand"},
    {"dt": "Delivery Zone"},
    {"dt": "Payment Mode"},
    # ── CMS Supporting Data ──
    {"dt": "FAQ Category"},
    {"dt": "FAQ Item"},
    {"dt": "Offer"},
    {"dt": "Popular Location"},
    {"dt": "Navigation Item"},
    {"dt": "Banner"},
    {"dt": "Site Page"},
    # ── Loyalty & Membership ──
    {"dt": "Loyalty Program"},
    {"dt": "Loyalty Tier"},
    {"dt": "Membership Plan"},
    {"dt": "Membership Benefit"},
    # ── Product Schema (for structure, not data) ──
    {"dt": "Product Specification"},
    {"dt": "Product Variant Attribute"},
    {"dt": "Product Media"},
    {"dt": "Product Price"},
    # ── Order Child Tables ──
    {"dt": "Order Item"},
    {"dt": "Order Tax"},
    {"dt": "Cart Item"},
    # ── Vendor Schema ──
    {"dt": "Vendor Warehouse"},
    {"dt": "Vendor Barcode Index"},
    # ── Notification Device ──
    {"dt": "SM Notification Device"},
    # ── Export & Reporting ──
    {"dt": "SM Feature Flag"},
]

# ── Permissions ───────────────────────────────────────────────────────────────
has_permission = {
    "Cart":              "saathimart.api.auth.has_cart_permission",
    "Order":             "saathimart.api.auth.has_order_permission",
    "Address":           "saathimart.api.auth.has_address_permission",
    "Wishlist":          "saathimart.api.auth.has_wishlist_permission",
    "SM Product Review": "saathimart.api.auth.has_review_permission",
}

permission_query_conditions = {
    "Address":           "saathimart.api.auth.get_address_permission_query_conditions",
    "Wishlist":          "saathimart.api.auth.get_wishlist_permission_query_conditions",
    "SM Product Review": "saathimart.api.auth.get_review_permission_query_conditions",
}

# ── Boot info ─────────────────────────────────────────────────────────
extend_bootinfo = "saathimart.api.auth.extend_bootinfo"

# ── Fixtures for new DocTypes ───────────────────────────────────────────────
# Delivery slots are configuration data — export across environments
fixtures.extend([
    {"dt": "Delivery Time Slot"},
    {"dt": "Delivery Slot Booking"},
])
