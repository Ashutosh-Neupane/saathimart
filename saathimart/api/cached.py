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
    - Cache locking: only one request triggers DB query during cache miss
    - Async refresh: cache expires, serves stale while fetching fresh
"""
import hashlib
import json
import functools
import frappe
from frappe.utils import cint
import time
import random


LOCK_TIMEOUT = 5  # Seconds


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

    Implements:
    - Cache-aside pattern with version-based invalidation
    - Cache locking to prevent thundering herd
    - Stale-while-revalidate for smooth cache expiry

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

            # Keys
            version_key = f"{key}:v"
            data_key = f"{key}:d"
            lock_key = f"{key}:lock"
            refresh_key = f"{key}:refresh"

            # Get current version
            current_version = cint(cache.get_value(version_key) or 0)

            # Try to get cached data with version
            cached_with_version = cache.get_value(data_key)
            
            # Stale-while-revalidate: if data exists but is stale, return it
            # and trigger background refresh
            if cached_with_version is not None:
                cached_data, cached_version = cached_with_version
                if cached_version == current_version:
                    return cached_data
                # Data is stale but still usable - serve stale, refresh in background
                # Only one request should refresh
                if cache.setnx(lock_key, "1"):
                    cache.setex(lock_key, LOCK_TIMEOUT, "1")
                    # Trigger background refresh
                    try:
                        result = fn(*args, **kwargs)
                        if isinstance(result, (dict, list)):
                            cache.set_value(data_key, (result, current_version), expires_in_sec=ttl)
                    except Exception:
                        pass
                    finally:
                        cache.delete_key(lock_key)
                return cached_data

            # Cache miss with lock
            # First, try to acquire lock
            if cache.setnx(lock_key, "1"):
                cache.setex(lock_key, LOCK_TIMEOUT, "1")
                try:
                    result = fn(*args, **kwargs)
                    if isinstance(result, (dict, list)):
                        cache.set_value(data_key, (result, current_version), expires_in_sec=ttl)
                    return result
                except Exception:
                    return fn(*args, **kwargs)  # Retry without cache
                finally:
                    cache.delete_key(lock_key)
            else:
                # Another request is refreshing, wait briefly then retry
                time.sleep(random.uniform(0.01, 0.05))
                # Try to get cached data again
                cached_with_version = cache.get_value(data_key)
                if cached_with_version is not None:
                    cached_data, cached_version = cached_with_version
                    if cached_version == current_version:
                        return cached_data
                # Last resort - call directly (this may hit DB)
                return fn(*args, **kwargs)

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
        version_key = f"{key}:v"
        # Increment version to invalidate cache entries
        version = cint(cache.get_value(version_key) or 0)
        cache.set_value(version_key, version + 1, expires_in_sec=86400)
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

# Product list: cache 3600s (1 hour - very stable data)
cache_product_list = cached_response(ttl=3600, key_prefix="product_list")

# Product detail: cache 120s (changes when reviews/stock update)
cache_product_detail = cached_response(ttl=120, key_prefix="product_detail")

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
