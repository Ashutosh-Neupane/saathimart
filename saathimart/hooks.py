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
    "saathimart.api.custom_fields.ensure_custom_fields",
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
            "saathimart.api.storefront_cache.on_order_changed",
        ],
        "on_update": [
            "saathimart.events.publisher.on_order_updated",
            "saathimart.api.order_events.on_order_paid",
            "saathimart.api.audit.log_order_update",
            "saathimart.api.storefront_cache.on_order_changed",
        ],
    },
    "Payment Log": {
        "after_insert": [
            "saathimart.api.accounting.on_payment_log_created",
        ],
    },
    "Product": {
        "after_insert": [
            "saathimart.events.publisher.on_product_created",
            "saathimart.api.audit.log_product_update",
            "saathimart.api.vector_search.index_product",
            "saathimart.api.storefront_cache.on_product_changed",
        ],
        "on_update": [
            "saathimart.events.publisher.on_product_updated",
            "saathimart.api.audit.log_product_update",
            "saathimart.api.vector_search.index_product",
            "saathimart.api.storefront_cache.on_product_changed",
        ],
        "on_trash": [
            "saathimart.events.publisher.on_product_deleted",
            "saathimart.api.storefront_cache.on_product_changed",
        ],
    },
    "Review": {
        "on_update": [
            "saathimart.api.reviews._update_product_rating",
            "saathimart.api.storefront_cache.on_review_changed",
        ],
        "on_trash": [
            "saathimart.api.reviews._update_product_rating",
            "saathimart.api.storefront_cache.on_review_changed",
        ],
    },
    "SM Audit Log": {
        "after_insert": "saathimart.api.audit.log_audit_entry",
    },
    "Vendor Listing": {
        "after_insert": [
            "saathimart.events.publisher.on_vendor_listing_changed",
            "saathimart.api.storefront_cache.on_vendor_listing_changed",
        ],
        "on_update": [
            "saathimart.events.publisher.on_vendor_listing_changed",
            "saathimart.api.storefront_cache.on_vendor_listing_changed",
        ],
        "on_trash": [
            "saathimart.events.publisher.on_vendor_listing_changed",
            "saathimart.api.storefront_cache.on_vendor_listing_changed",
        ],
    },
    "Vendor Stock": {
        "after_insert": "saathimart.api.storefront_cache.on_vendor_stock_changed",
        "on_update":    "saathimart.api.storefront_cache.on_vendor_stock_changed",
        "on_trash":     "saathimart.api.storefront_cache.on_vendor_stock_changed",
    },
    "Stock Ledger Entry": {
        # SLE controller updates Product.stock_qty via raw db.set_value —
        # these hooks are the only invalidation for those writes.
        "after_insert": "saathimart.api.storefront_cache.on_stock_ledger_entry",
    },
    "Brand": {
        "after_insert": [
            "saathimart.api.storefront_cache.on_brand_changed",
        ],
        "on_update": [
            "saathimart.api.storefront_cache.on_brand_changed",
        ],
        "on_trash": [
            "saathimart.api.storefront_cache.on_brand_changed",
        ],
    },
    "Category": {
        "after_insert": [
            "saathimart.api.storefront_cache.on_category_changed",
        ],
        "on_update": [
            "saathimart.api.storefront_cache.on_category_changed",
        ],
        "on_trash": [
            "saathimart.api.storefront_cache.on_category_changed",
        ],
    },
    "Coupon": {
        # Cached cart totals embed coupon discount math — bust sm_totals:* on
        # any coupon edit (validate/usage-limit changes).
        "on_update": "saathimart.api.storefront_cache.on_coupon_changed",
        "on_trash": "saathimart.api.storefront_cache.on_coupon_changed",
    },
    "Vendor": {
        "on_update": "saathimart.events.publisher.on_vendor_updated",
    },
    "Site Config": {
        "on_update": [
            "saathimart.api.cms._bust_site_config_cache",
            "saathimart.api.storefront_cache.on_cms_changed",
        ],
    },
    "Navigation Item": {
        "on_update": [
            "saathimart.api.cms._bust_navigation_cache",
            "saathimart.api.storefront_cache.on_cms_changed",
        ],
    },
    "Banner": {
        "on_update": [
            "saathimart.api.cms._bust_banner_cache",
            "saathimart.api.storefront_cache.on_cms_changed",
        ],
    },
    "Site Page": {
        "on_update": [
            "saathimart.api.cms._bust_page_cache",
            "saathimart.api.storefront_cache.on_site_page_changed",
        ],
    },
    "Blog Post": {
        "on_update": [
            "saathimart.api.cms._bust_blog_cache",
            "saathimart.api.storefront_cache.on_cms_changed",
        ],
    },
    "FAQ Category": {
        "on_update": [
            "saathimart.api.cms._bust_faq_category_cache",
            "saathimart.api.storefront_cache.on_faq_changed",
        ],
    },
    "FAQ Item": {
        "on_update": [
            "saathimart.api.cms._bust_faq_item_cache",
            "saathimart.api.storefront_cache.on_faq_changed",
        ],
    },
    "Offer": {
        "on_update": [
            "saathimart.api.cms._bust_offer_cache_on_update",
            "saathimart.api.storefront_cache.on_offer_changed",
        ],
    },
    "Popular Location": {
        "on_update": [
            "saathimart.api.cms._bust_location_cache_on_update",
            "saathimart.api.storefront_cache.on_cms_changed",
        ],
    },
    "Hero Slide": {
        "on_update": [
            "saathimart.api.cms._bust_home_content_cache",
            "saathimart.api.storefront_cache.on_cms_changed",
        ],
    },
    "Seasonal Banner": {
        "on_update": [
            "saathimart.api.cms._bust_home_content_cache",
            "saathimart.api.storefront_cache.on_cms_changed",
        ],
    },
    "Trust Badge": {
        "on_update": [
            "saathimart.api.cms._bust_home_content_cache",
            "saathimart.api.storefront_cache.on_cms_changed",
        ],
    },
    "Product Rail Heading": {
        "on_update": [
            "saathimart.api.cms._bust_home_content_cache",
            "saathimart.api.storefront_cache.on_cms_changed",
        ],
    },
    "Homepage Settings": {
        "on_update": [
            "saathimart.api.cms._bust_home_content_cache",
            "saathimart.api.storefront_cache.on_cms_changed",
        ],
    },
    "Website Content": {
        "on_update": [
            "saathimart.api.cms._bust_content_cache",
            "saathimart.api.storefront_cache.on_cms_changed",
        ],
    },
    "About Us": {
        "on_update": [
            "saathimart.api.cms._bust_static_page_cache",
            "saathimart.api.storefront_cache.on_cms_changed",
        ],
    },
    "Terms Page": {
        "on_update": [
            "saathimart.api.cms._bust_static_page_cache",
            "saathimart.api.storefront_cache.on_cms_changed",
        ],
    },
    "Privacy Page": {
        "on_update": [
            "saathimart.api.cms._bust_static_page_cache",
            "saathimart.api.storefront_cache.on_cms_changed",
        ],
    },
    "Cookies Page": {
        "on_update": [
            "saathimart.api.cms._bust_static_page_cache",
            "saathimart.api.storefront_cache.on_cms_changed",
        ],
    },
    "Careers Page": {
        "on_update": [
            "saathimart.api.cms._bust_static_page_cache",
            "saathimart.api.storefront_cache.on_cms_changed",
        ],
    },
    "Partner Page": {
        "on_update": [
            "saathimart.api.cms._bust_static_page_cache",
            "saathimart.api.storefront_cache.on_cms_changed",
        ],
    },
    "Rider Page": {
        "on_update": [
            "saathimart.api.cms._bust_static_page_cache",
            "saathimart.api.storefront_cache.on_cms_changed",
        ],
    },
}

# ── Scheduled tasks ───────────────────────────────────────────────────────────
scheduler_events = {
    # 00:00 sharp — the nightly promotional close: the day's redeemed coupons
    # and loyalty points hit accounting as per-vendor liability JEs.
    "cron": {
        "0 0 * * *": [
            "saathimart.api.daily_promotions.consolidate_daily_promotions",
        ],
    },
    "daily": [
        "saathimart.api.loyalty.expire_old_points",
        "saathimart.api.loyalty.check_birthday_rewards",
        "saathimart.api.membership.expire_memberships",
        # Auto-renew auto_renew-flagged memberships + expiry reminders
        "saathimart.api.membership.process_membership_renewals",
        "saathimart.api.archival.archive_old_data",
        # Purge expired OTP rows from the verification store.
        "saathimart.api.auth_full.cleanup_expired_verifications",
        # Keep listings of Pending/Suspended vendors out of the live catalog.
        "saathimart.api.vendor_hygiene.deactivate_listings_of_inactive_vendors",
        # Active vendors without a site URL can never receive events — suspend them.
        "saathimart.api.vendor_hygiene.suspend_unroutable_vendors",
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
        # Daily sync to ERPNext — pushes paid orders, customers, invoices.
        # Only runs when Settings.erpnext_sync_enabled = 1.
        "saathimart.api.erpnext_sync.run_daily_sync",
    ],
    "hourly": [
        "saathimart.api.cart.expire_abandoned_carts",
        "saathimart.events.publisher.flush_failed_webhooks",
        "saathimart.api.reconciliation.reconcile_stock_hourly",
        # Stock snapshot sync: send full stock state to each vendor
        # (reconciliation checks individual products; snapshot sends everything)
        "saathimart.api.stock_snapshot.sync_all_stock_snapshots",
        # (removed) saathimart.api.stock.sync_vendor_listing_stock — the
        # Vendor Listing qty columns it kept refreshed are gone; Vendor
        # Stock is the single source of truth and all readers join it
        # directly.
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
# ── Fixtures ─────────────────────────────────────────────────────────────────
# ONLY include structural data that must exist for the app to work.
# Business data should be seeded via admin UI or migration scripts.
#
# DO NOT include:
#   - Products, Categories, Brands (business data)
#   - Vendors, Warehouses (business data)
#   - CMS content (Hero Slides, Offers, Banners - marketing data)
#   - Payment Modes, Delivery Zones (business configuration)
#   - Loyalty/Membership config (business rules)
#
# WHY: Fixtures are exported on every `bench migrate` and overwrite existing data.
#      Business data should be managed via UI, not overwritten on each migration.
fixtures = [
    # ── Roles (required for permissions to work) ──
    {"dt": "Role", "filters": [["name", "in", [
        "SM Admin", "SM Vendor", "SM Delivery", "SM Customer", "Website Manager",
    ]]]},
    
    # ── Core App Configuration (not business data) ──
    {"dt": "SaathiMart Settings"},           # App-level settings
    {"dt": "Site Config"},        # Site metadata
    {"dt": "Homepage Settings"},  # Homepage structure (not content)
    
    # ── DocType Definitions (child tables) ──
    {"dt": "Order Item"},
    {"dt": "Order Tax"},
    {"dt": "Cart Item"},
    {"dt": "Vendor Warehouse"},
    {"dt": "SM Notification Device"},
]

# ── Permissions ───────────────────────────────────────────────────────────────
has_permission = {
    "Cart":              "saathimart.api.auth.has_cart_permission",
    "Order":             "saathimart.api.auth.has_order_permission",
    "Address":           "saathimart.api.auth.has_address_permission",
    "Wishlist":          "saathimart.api.auth.has_wishlist_permission",
    "Review": "saathimart.api.auth.has_review_permission",
}

permission_query_conditions = {
    "Address":           "saathimart.api.auth.get_address_permission_query_conditions",
    "Wishlist":          "saathimart.api.auth.get_wishlist_permission_query_conditions",
    "Review":            "saathimart.api.auth.get_review_permission_query_conditions",
}

# ── Boot info ─────────────────────────────────────────────────────────
extend_bootinfo = "saathimart.api.auth.extend_bootinfo"

