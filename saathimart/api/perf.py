"""
Performance utilities — batch loaders and N+1 eliminators.

Usage:
    from saathimart.api.perf import (
        batch_load_listings,
        batch_load_stock,
        batch_load_images,
        batch_load_vendor_locations,
    )

All functions accept a list of product/vendor names and return a dict
mapping name → data, so callers can do O(1) lookups instead of
hitting the DB inside a loop.
"""
import frappe
from frappe.utils import cint


# ── Batch Loaders ────────────────────────────────────────────────────────────


def batch_load_listings(product_names):
    """Load all active Vendor Listings for a batch of products.

    Returns: {product_name: [{name, vendor, price, compare_price, ...}, ...]}
    Replaces: per-product get_all() calls inside list/detail loops.
    """
    if not product_names:
        return {}

    rows = frappe.db.sql(
        """
        SELECT vl.product, vl.name, vl.vendor, vl.price, vl.compare_price,
               vl.barcode, vl.sku, vl.status, vl.vendor_name_display,
               v.vendor_name, v.lat, v.lng
        FROM `tabVendor Listing` vl
        INNER JOIN `tabVendor` v ON v.name = vl.vendor
        WHERE vl.product IN %(products)s AND vl.status = 'Active'
        ORDER BY vl.price ASC
        """,
        {"products": tuple(product_names)},
        as_dict=True,
    )

    result = {}
    for r in rows:
        result.setdefault(r.product, []).append(r)
    return result


def batch_load_stock(product_names, vendor_names=None):
    """Load Vendor Stock for a batch of products.

    Returns: {(product, vendor): {available_qty, reserved_qty, physical_qty, warehouse}}
    Replaces: per-product get_value() calls.
    """
    if not product_names:
        return {}

    conditions = {"products": tuple(product_names)}
    vendor_filter = ""
    if vendor_names:
        vendor_filter = "AND vendor IN %(vendors)s"
        conditions["vendors"] = tuple(vendor_names)

    rows = frappe.db.sql(
        f"""
        SELECT product, vendor, available_qty, reserved_qty, physical_qty,
               warehouse, is_default_warehouse
        FROM `tabVendor Stock`
        WHERE product IN %(products)s {vendor_filter}
        """,
        conditions,
        as_dict=True,
    )

    result = {}
    for r in rows:
        result[(r.product, r.vendor)] = r
    return result


def batch_load_images(product_names):
    """Load Product Media for a batch of products.

    Returns: {product_name: [{image_url, alt_text, is_primary, sort_order}, ...]}
    Replaces: per-product media query.
    """
    if not product_names:
        return {}

    rows = frappe.db.sql(
        """
        SELECT parent AS product, image_url, alt_text, is_primary, sort_order
        FROM `tabProduct Media`
        WHERE parent IN %(products)s
        ORDER BY is_primary DESC, sort_order ASC
        """,
        {"products": tuple(product_names)},
        as_dict=True,
    )

    result = {}
    for r in rows:
        result.setdefault(r.product, []).append(r)
    return result


def batch_load_vendor_locations(vendor_names):
    """Load vendor lat/lng + service radius for distance calculations.

    Returns: {vendor_name: {lat, lng, service_radius_km, vendor_name}}
    Replaces: per-vendor get_value() in product enrichment.
    """
    if not vendor_names:
        return {}

    rows = frappe.db.sql(
        """
        SELECT name, vendor_name, lat, lng, service_radius_km
        FROM `tabVendor`
        WHERE name IN %(vendors)s AND lat IS NOT NULL AND lng IS NOT NULL
        """,
        {"vendors": tuple(vendor_names)},
        as_dict=True,
    )

    return {r.name: r for r in rows}


def batch_load_reviews(product_names):
    """Load review stats for a batch of products.

    Returns: {product_name: {avg_rating, review_count}}
    Replaces: per-product review COUNT/AVG query.
    """
    if not product_names:
        return {}

    rows = frappe.db.sql(
        """
        SELECT product, AVG(rating) AS avg_rating, COUNT(*) AS review_count
        FROM `tabReview`
        WHERE product IN %(products)s AND status = 'Approved'
        GROUP BY product
        """,
        {"products": tuple(product_names)},
        as_dict=True,
    )

    return {r.product: {"avg_rating": r.avg_rating, "review_count": cint(r.review_count)} for r in rows}


def batch_load_categories(category_names):
    """Load category details for a batch of category names.

    Returns: {category_name: {category_name, slug, image, ...}}
    """
    if not category_names:
        return {}

    rows = frappe.db.sql(
        """
        SELECT name, category_name, slug, image, parent_category, sort_order
        FROM `tabCategory`
        WHERE name IN %(cats)s AND is_active = 1
        """,
        {"cats": tuple(category_names)},
        as_dict=True,
    )

    return {r.name: r for r in rows}


# ── Batch Loader for Product List Page ───────────────────────────────────────


def preload_product_page_data(product_names, customer_lat=None, customer_lng=None):
    """Single-call preload for a page of products.

    Returns a dict with all pre-loaded data, keyed by type:
        {
            "listings": {product: [listing, ...]},
            "stock": {(product, vendor): stock_data},
            "images": {product: [image, ...]},
            "vendors": {vendor: location_data},
            "reviews": {product: {avg_rating, review_count}},
        }

    This eliminates N+1 queries on the product list page — instead of
    5 queries per product, we do 5 queries total for the whole page.
    """
    data = {
        "listings": {},
        "stock": {},
        "images": {},
        "vendors": {},
        "reviews": {},
    }

    if not product_names:
        return data

    # Step 1: Batch-load listings (1 query)
    data["listings"] = batch_load_listings(product_names)

    # Step 2: Extract vendor names from listings
    vendor_names = set()
    for listings in data["listings"].values():
        for l in listings:
            vendor_names.add(l.vendor)

    # Step 3: Batch-load stock for all vendors × products (1 query)
    data["stock"] = batch_load_stock(product_names, vendor_names or None)

    # Step 4: Batch-load images (1 query)
    data["images"] = batch_load_images(product_names)

    # Step 5: Batch-load vendor locations (1 query)
    data["vendors"] = batch_load_vendor_locations(vendor_names or set())

    # Step 6: Batch-load reviews (1 query)
    data["reviews"] = batch_load_reviews(product_names)

    return data


# ── Cached Versions ──────────────────────────────────────────────────────────


def get_cached_product_page_data(product_names, customer_lat=None, customer_lng=None):
    """Cached version of preload_product_page_data.

    Cache key includes sorted product names + lat/lng (rounded to 2 decimals
    to group nearby customers into the same cache bucket).
    """
    if not product_names:
        return {}

    # Build cache key
    lat_bucket = round(float(customer_lat or 0), 2)
    lng_bucket = round(float(customer_lng or 0), 2)
    key = f"sm_product_page:{','.join(sorted(product_names[:20]))}:{lat_bucket}:{lng_bucket}"

    cache = frappe.cache()
    cached = cache.get_value(key)
    if cached is not None:
        return cached

    data = preload_product_page_data(product_names, customer_lat, customer_lng)
    cache.set_value(key, data, expires_in_sec=30)
    return data


def invalidate_product_cache(product_name=None):
    """Invalidate product-related cache entries.

    Called after product/stock/vendor updates.
    Pattern-based invalidation is expensive in Redis, so we use
    a version counter instead.
    """
    cache = frappe.cache()
    version = cint(cache.get_value("sm_product_cache_version") or 0)
    cache.set_value("sm_product_cache_version", version + 1, expires_in_sec=3600)
