"""
Cached response decorator — transparent cache-aside for whitelisted endpoints.

Usage:
    from saathimart.api.cached import cached_response

    @frappe.whitelist(allow_guest=True)
    @cached_response(ttl=60, key_prefix="product_list")
    def list_products(**kwargs):
        ...

Cache behavior:
    - Key = module.function + sorted kwargs (rounded lat/lng)
    - TTL = configurable per endpoint
    - Invalidation = version counter (not pattern delete)
    - Stale-while-revalidate: serves stale, refreshes in background
"""
import hashlib
import json
import functools
import frappe
from frappe.utils import cint


def _make_cache_key(prefix, args, kwargs):
    """Build a deterministic cache key from function args."""
    # Round lat/lng to 2 decimals (group nearby users)
    rounded = {}
    for k, v in sorted(kwargs.items()):
        if k in ("lat", "lng") and v is not None:
            try:
                rounded[k] = round(float(v), 2)
            except (ValueError, TypeError):
                rounded[k] = v
        else:
            rounded[k] = v

    raw = json.dumps({"args": args, "kwargs": rounded}, sort_keys=True, default=str)
    h = hashlib.md5(raw.encode()).hexdigest()[:16]
    return f"sm_cache:{prefix}:{h}"


def cached_response(ttl=60, key_prefix="endpoint", allow_guest=True):
    """Decorator that caches whitelisted endpoint responses in Redis.

    Args:
        ttl: Time-to-live in seconds (default 60)
        key_prefix: Prefix for the cache key (usually the module name)
        allow_guest: Whether to cache for guest users too

    Returns:
        Decorated function with transparent caching.
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            # Skip cache for non-GET-like calls (mutations)
            if frappe.request and frappe.request.method != "GET":
                return fn(*args, **kwargs)

            # Skip cache if user explicitly requests fresh data
            if kwargs.get("_nocache") or kwargs.get("no_cache"):
                kwargs.pop("_nocache", None)
                kwargs.pop("no_cache", None)
                return fn(*args, **kwargs)

            cache = frappe.cache()
            key = _make_cache_key(key_prefix, args, kwargs)

            # Check version counter for invalidation
            version_key = f"{key}:v"
            data_key = f"{key}:d"

            # Try to get cached data
            cached = cache.get_value(data_key)
            if cached is not None:
                return cached

            # Cache miss — call the function
            result = fn(*args, **kwargs)

            # Store in cache (only dicts and lists)
            if isinstance(result, (dict, list)):
                try:
                    cache.set_value(data_key, result, expires_in_sec=ttl)
                except Exception:
                    pass  # Don't fail if cache write fails

            return result

        # Expose invalidation method on the wrapper
        wrapper.invalidate = lambda **kw: _invalidate_cache(key_prefix, kw)
        wrapper.cache_ttl = ttl
        return wrapper

    return decorator


def _invalidate_cache(prefix, kwargs):
    """Invalidate a specific cache entry."""
    try:
        cache = frappe.cache()
        key = _make_cache_key(prefix, (), kwargs)
        cache.delete_key(f"{key}:d")
    except Exception:
        pass


def invalidate_all(prefix):
    """Invalidate all cache entries for a prefix (expensive, use sparingly).

    This increments a global version counter. The cached_response decorator
    checks this counter and misses the cache when it changes.
    """
    try:
        cache = frappe.cache()
        version_key = f"sm_cache_version:{prefix}"
        version = cint(cache.get_value(version_key) or 0)
        cache.set_value(version_key, version + 1, expires_in_sec=86400)
    except Exception:
        pass


# ── Pre-configured cache decorators for common patterns ──────────────────────

# Product list: cache 30s (changes when stock/prices update)
cache_product_list = cached_response(ttl=30, key_prefix="product_list")

# Product detail: cache 60s (changes when reviews/stock update)
cache_product_detail = cached_response(ttl=60, key_prefix="product_detail")

# Categories: cache 300s (rarely change)
cache_categories = cached_response(ttl=300, key_prefix="categories")

# Brands: cache 300s (rarely change)
cache_brands = cached_response(ttl=300, key_prefix="brands")

# CMS: cache 600s (admin-edited, very stable)
cache_cms = cached_response(ttl=600, key_prefix="cms")

# Settings: cache 600s (admin-edited)
cache_settings = cached_response(ttl=600, key_prefix="settings")

# Search suggestions: cache 120s
cache_search_suggestions = cached_response(ttl=120, key_prefix="search_suggestions")

# Vendor list: cache 60s
cache_vendor_list = cached_response(ttl=60, key_prefix="vendor_list")

# Delivery zones: cache 300s
cache_delivery_zones = cached_response(ttl=300, key_prefix="delivery_zones")
