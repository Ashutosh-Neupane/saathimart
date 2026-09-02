"""
Cache-Control headers for CDN caching.

Sets proper Cache-Control and ETag headers on responses so that:
1. Product images are CDN-cached (Cache-Control: public, max-age=86400)
2. CMS content is short-lived cached (Cache-Control: public, max-age=300)
3. API responses have no-cache (dynamic content)

This is especially important for Nepal where 2G/3G connections are common.
"""
import hashlib

import frappe
from frappe import _


def set_cache_headers(max_age=0, public=False, must_revalidate=False, immutable=False):
    """
    Set Cache-Control headers on the current response.
    
    Args:
        max_age: Max age in seconds (0 = no-cache, 86400 = 1 day, 604800 = 1 week)
        public: If True, allow CDN caching
        must_revalidate: If True, cache must revalidate after max_age
        immutable: If True, content never changes (use with versioned URLs)
    """
    if not frappe.response:
        return
    
    headers = []
    
    # Build Cache-Control value
    cache_parts = []
    if public:
        cache_parts.append("public")
    else:
        cache_parts.append("private")
    
    if max_age > 0:
        cache_parts.append(f"max-age={max_age}")
    
    if must_revalidate:
        cache_parts.append("must-revalidate")
    
    if immutable:
        cache_parts.append("immutable")
    
    cache_control = ", ".join(cache_parts)
    
    # Set headers
    frappe.response["Cache-Control"] = cache_control
    
    # Add ETag for cache validation
    if max_age > 0:
        etag = _generate_etag()
        frappe.response["ETag"] = f'"{etag}"'
        frappe.response["Vary"] = "Accept-Encoding"


def _generate_etag():
    """Generate a simple ETag based on current time and request path."""
    import time
    path = frappe.request.path if frappe.request else "/"
    timestamp = int(time.time() / 300)  # Changes every 5 minutes
    return hashlib.md5(f"{path}:{timestamp}".encode()).hexdigest()[:16]


def set_image_cache_headers():
    """
    Set long-lived cache headers for product images.
    
    Images are versioned (Frappe adds file hash), so we can cache aggressively.
    CDN edge: 1 day, browser: 1 week.
    """
    set_cache_headers(
        max_age=604800,  # 1 week
        public=True,
        must_revalidate=True,
    )


def set_cms_cache_headers():
    """
    Set short-lived cache headers for CMS content.
    
    CMS content changes frequently, but doesn't need to be real-time.
    CDN edge: 5 minutes, browser: 1 minute.
    """
    set_cache_headers(
        max_age=300,  # 5 minutes
        public=True,
        must_revalidate=True,
    )


def set_api_cache_headers():
    """
    Set no-cache headers for dynamic API responses.
    
    API responses should never be cached by CDN or browser.
    """
    set_cache_headers(
        max_age=0,
        public=False,
    )


def set_static_cache_headers():
    """
    Set long-lived cache headers for static assets.
    
    Static assets (JS, CSS, fonts) should be cached aggressively.
    Use with versioned filenames (e.g., app.abc123.js).
    """
    set_cache_headers(
        max_age=31536000,  # 1 year
        public=True,
        immutable=True,
    )


# Middleware-style hooks for automatic cache headers

def apply_cache_headers(doc, method):
    """
    Frappe hook: Apply cache headers based on request path.
    
    Add this to hooks.py:
        before_request = ["saathimart.api.cache_headers.apply_cache_headers"]
    """
    if not frappe.request:
        return
    
    path = frappe.request.path
    
    # Product images
    if "/files/" in path and any(ext in path.lower() for ext in [".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"]):
        set_image_cache_headers()
        return
    
    # API endpoints - no cache
    if path.startswith("/api/"):
        set_api_cache_headers()
        return
    
    # CMS content - short cache
    if any(segment in path for segment in ["/content", "/banners", "/pages", "/blog"]):
        set_cms_cache_headers()
        return
    
    # Static assets - long cache
    if any(segment in path for segment in ["/assets", "/static", "/_next"]):
        set_static_cache_headers()
        return


def bust_image_cache(file_url):
    """
    Bust cache for a specific image by adding a version query parameter.
    
    Call this when an image is updated to force CDN/browser to fetch the new version.
    
    Args:
        file_url: The file URL to bust cache for
    """
    import re
    # Add or update version parameter
    version = int(frappe.utils.now_timestamp())
    if "?" in file_url:
        # Replace existing v= parameter
        file_url = re.sub(r'v=\d+', f'v={version}', file_url)
    else:
        file_url = f"{file_url}?v={version}"
    return file_url
