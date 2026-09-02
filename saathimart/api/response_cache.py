"""API Response Caching for Saathimart.

Caches API responses to reduce database load and improve response times.
"""
import frappe
import json
import hashlib
from frappe.utils import now_datetime
from functools import wraps


def generate_cache_key(endpoint, params):
    """Generate a cache key from endpoint and params."""
    # Sort params for consistent keys
    sorted_params = json.dumps(params, sort_keys=True)
    param_hash = hashlib.md5(sorted_params.encode()).hexdigest()[:12]
    return f"api_response:{endpoint}:{param_hash}"


def cache_response(ttl=60):
    """Decorator to cache API response."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            # Generate cache key
            cache_key = generate_cache_key(fn.__name__, kwargs)
            
            # Try to get from cache
            cache = frappe.cache()
            cached = cache.get_value(cache_key)
            if cached:
                return json.loads(cached)
            
            # Execute function and cache result
            result = fn(*args, **kwargs)
            cache.set_value(cache_key, json.dumps(result), expires_in_sec=ttl)
            return result
        
        return wrapper
    return decorator


def invalidate_cache(pattern="*"):
    """Invalidate cached responses."""
    cache = frappe.cache()
    if pattern == "*":
        # Invalidate all API cache
        keys = cache.get_keys("api_response:*")
        for key in keys:
            cache.delete_key(key)
    else:
        # Invalidate specific pattern
        keys = cache.get_keys(f"api_response:{pattern}:*")
        for key in keys:
            cache.delete_key(key)


# Predefined cache invalidation functions
def invalidate_product_cache(product_name=None):
    """Invalidate product-related caches."""
    if product_name:
        invalidate_cache(f"product:{product_name}")
    else:
        invalidate_cache("product:*")


def invalidate_catalog_cache():
    """Invalidate catalog/product list caches."""
    invalidate_cache("catalog:*")


def invalidate_search_cache():
    """Invalidate search caches."""
    invalidate_cache("search:*")