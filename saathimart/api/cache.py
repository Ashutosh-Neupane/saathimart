"""
Redis caching layer for hot product endpoints. Reduces DB load by serving
cached results for read-heavy endpoints like list_products and get_banners.

Cache strategy:
  - Short TTL (30s) for dynamic results (search, filters)
  - Medium TTL (60s) for product detail pages
  - Long TTL (300s) for static data (banners, navigation)
  - Invalidation on stock/price changes via doc_events
"""
import frappe
from functools import wraps

# Cache key prefixes
PREFIX_PRODUCT = "sm_product:"
PREFIX_LISTING = "sm_listing:"
PREFIX_BANNER = "sm_banner:"
PREFIX_NAV = "sm_nav:"
PREFIX_SEARCH = "sm_search:"

# TTLs in seconds
TTL_PRODUCT_DETAIL = 60
TTL_LISTING = 30
TTL_BANNER = 300
TTL_NAV = 300
TTL_SEARCH = 30


def cached(prefix, ttl, key_fn=None):
    """Decorator: cache a function's return value in Redis.

    key_fn: optional function(*args, **kwargs) -> str for custom cache keys.
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            cache = frappe.cache()
            if key_fn:
                cache_key = prefix + key_fn(*args, **kwargs)
            else:
                # Default: prefix + function name + sorted args hash
                import hashlib
                arg_str = str(args) + str(sorted(kwargs.items()))
                arg_hash = hashlib.md5(arg_str.encode()).hexdigest()[:12]
                cache_key = f"{prefix}{fn.__name__}:{arg_hash}"

            result = cache.get_value(cache_key)
            if result is not None:
                return result

            result = fn(*args, **kwargs)
            if result is not None:
                cache.set_value(cache_key, result, expires_in_sec=ttl)
            return result

        wrapper.invalidate = lambda *a, **kw: frappe.cache().delete_key(
            prefix + (key_fn(*a, **kw) if key_fn else f"{fn.__name__}:default")
        )
        wrapper.cache_prefix = prefix
        return wrapper
    return decorator


def invalidate_product(product_name):
    """Invalidate all caches related to a product."""
    cache = frappe.cache()
    # Delete product detail cache
    cache.delete_key(f"{PREFIX_PRODUCT}{product_name}")
    # Delete listing caches (we can't know all keys, so delete by pattern)
    # Redis doesn't support pattern delete efficiently, so we use a version key
    cache.delete_key("sm_listing_version")


def invalidate_stock(vendor=None, product=None):
    """Invalidate stock-related caches when stock changes."""
    cache = frappe.cache()
    cache.delete_key("sm_listing_version")
    if product:
        cache.delete_key(f"{PREFIX_PRODUCT}{product}")


def get_listing_version():
    """Get or increment the listing cache version."""
    cache = frappe.cache()
    key = "sm_listing_version"
    version = cache.get_value(key)
    if version is None:
        version = 0
        cache.set_value(key, version, expires_in_sec=TTL_LISTING)
    return version
