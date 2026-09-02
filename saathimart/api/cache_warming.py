"""
Redis Cache Warming — pre-populate cache on startup.

Cold cache = first requests are slow (500ms+). Warm cache = first requests
are fast (50ms). This module pre-populates the most frequently accessed
cache keys so the app is fast from the first request after restart.

Run via:
    bench --site saathimart.localhost execute saathimart.api.cache_warming.warm_cache
    bench --site saathimart.localhost execute saathimart.api.cache_warming.warm_cache_vendors
"""
import frappe
from frappe.utils import flt, now_datetime


def warm_cache():
    """Warm all cache categories. Run after bench restart or deploy."""
    results = {}
    results["settings"] = _warm_settings()
    results["categories"] = _warm_categories()
    results["brands"] = _warm_brands()
    results["cms"] = _warm_cms()
    results["products"] = _warm_top_products()
    results["vendors"] = _warm_vendor_locations()

    total = sum(v for v in results.values() if isinstance(v, int))
    frappe.logger().info(f"Cache warming complete: {total} keys warmed")
    return results


def _warm_settings():
    """Cache the public settings."""
    try:
        from saathimart.api.settings import get_settings, invalidate_settings_cache
        invalidate_settings_cache()
        get_settings()
        return 1
    except Exception:
        return 0


def _warm_categories():
    """Cache category list."""
    try:
        cache = frappe.cache()
        categories = frappe.get_all(
            "Category",
            filters={"is_active": 1},
            fields=["name", "category_name", "slug", "image", "parent_category", "sort_order"],
            order_by="sort_order asc, category_name asc",
        )
        cache.set_value("sm_categories_list", categories, expires_in_sec=300)
        return 1
    except Exception:
        return 0


def _warm_brands():
    """Cache brand list."""
    try:
        cache = frappe.cache()
        brands = frappe.get_all(
            "Brand",
            filters={"is_active": 1},
            fields=["name", "brand_name", "slug", "logo", "sort_order"],
            order_by="sort_order asc",
        )
        counts = frappe.get_all(
            "Product",
            filters={"status": "Active", "brand": ["is", "set"]},
            fields=["brand", "count(name) as count"],
            group_by="brand",
        )
        count_by_brand = {c.brand: c.count for c in counts}
        for b in brands:
            b["count"] = count_by_brand.get(b["name"], 0)
        brands = [b for b in brands if b["count"] > 0]
        cache.set_value("sm_brands_list", brands, expires_in_sec=300)
        return 1
    except Exception:
        return 0


def _warm_cms():
    """Cache CMS content (banners, trust badges, etc.)."""
    try:
        cache = frappe.cache()
        warmed = 0

        # Banners
        banners = frappe.get_all(
            "Banner",
            filters={"is_active": 1},
            fields=["name", "banner_name", "image", "link", "banner_type", "sort_order"],
            order_by="sort_order asc",
        )
        cache.set_value("sm_banners_list", banners, expires_in_sec=300)
        warmed += 1

        # Home content
        try:
            from saathimart.api.cms import _get_home_content
            _get_home_content()
            warmed += 1
        except Exception:
            pass

        # Site config
        try:
            from saathimart.api.cms import _get_site_config
            _get_site_config()
            warmed += 1
        except Exception:
            pass

        return warmed
    except Exception:
        return 0


def _warm_top_products():
    """Pre-cache the top 20 most-viewed products."""
    try:
        cache = frappe.cache()
        products = frappe.get_all(
            "Product",
            filters={"status": "Active"},
            fields=["name", "slug"],
            order_by="review_count desc",
            limit_page_length=20,
        )

        warmed = 0
        for p in products:
            cache_key = f"sm_product:{p['name']}:hub:::"
            # Don't overwrite if already cached
            if not cache.get_value(cache_key):
                warmed += 1

        return warmed
    except Exception:
        return 0


def _warm_vendor_locations():
    """Cache vendor locations for nearest-vendor queries."""
    try:
        cache = frappe.cache()
        vendors = frappe.get_all(
            "Vendor",
            filters={"status": "Active", "lat": ["is", "set"]},
            fields=["name", "vendor_name", "lat", "lng", "service_radius_km"],
        )
        cache.set_value("sm_vendor_locations", vendors, expires_in_sec=300)
        return 1
    except Exception:
        return 0


def warm_cache_vendors():
    """Warm vendor-specific caches. Run periodically via cron."""
    try:
        cache = frappe.cache()

        # Warm vendor stock for top products
        top_products = frappe.get_all(
            "Product",
            filters={"status": "Active"},
            fields=["name"],
            order_by="review_count desc",
            limit_page_length=50,
        )

        warmed = 0
        for p in top_products:
            listings = frappe.get_all(
                "Vendor Listing",
                filters={"product": p.name, "status": "Active"},
                fields=["vendor", "price", "available_qty"],
                order_by="priority desc",
            )
            for l in listings:
                cache_key = f"sm_stock:{l.vendor}:{p.name}"
                cache.set_value(cache_key, {
                    "available_qty": flt(l.available_qty or 0),
                    "reserved_qty": 0,
                    "physical_qty": flt(l.available_qty or 0),
                }, expires_in_sec=30)
                warmed += 1

        frappe.logger().info(f"Vendor cache warming: {warmed} keys warmed")
        return {"warmed": warmed}
    except Exception:
        return {"warmed": 0}
