"""
Request ID and rate limit headers for debugging and client throttling.

Adds:
1. X-Request-ID: Unique identifier for each request (for log correlation)
2. X-RateLimit-Limit: Maximum requests allowed per window
3. X-RateLimit-Remaining: Requests remaining in current window
4. X-RateLimit-Reset: Unix timestamp when the window resets
5. Retry-After: Seconds until the rate limit resets (only on 429 responses)

Usage:
    Add to hooks.py:
        before_request = ["saathimart.api.request_tracking.add_request_id"]
        after_request = ["saathimart.api.request_tracking.add_rate_limit_headers"]
"""
import time
import uuid

import frappe
from frappe import _


def _get_client_ip():
    """Get the client's IP address from request headers."""
    try:
        ip = frappe.get_request_header("X-Forwarded-For", "").split(",")[0].strip()
        if not ip:
            ip = frappe.get_request_header("X-Real-IP", "")
        if not ip and frappe.request:
            ip = getattr(frappe.request, "ip", "unknown") or "unknown"
    except Exception:
        ip = "unknown"
    return ip


def add_request_id():
    """
    Hook: Add X-Request-ID to request and response.
    
    Generates a unique request ID if not provided by client.
    This ID is used for log correlation across services.
    """
    if not frappe.request:
        return
    
    # Get or generate request ID
    request_id = frappe.get_request_header("X-Request-ID", "")
    if not request_id:
        request_id = str(uuid.uuid4())[:16]
    
    # Store in frappe.local for logging
    frappe.local.sm_request_id = request_id
    
    # Add to response headers (will be added in after_request)
    if frappe.response:
        frappe.response["X-Request-ID"] = request_id


def add_rate_limit_headers():
    """
    Hook: Add rate limit headers to response.
    
    Shows the client their current rate limit status so they can
    implement backoff logic.
    """
    if not frappe.response:
        return
    
    # Skip for non-API requests
    if not frappe.request:
        return
    
    path = frappe.request.path or ""
    if not path.startswith("/api/method/saathimart."):
        return
    
    # Get rate limit info from cache
    client_ip = _get_client_ip()
    cache_key = f"sm_rate_limit:{client_ip}"
    
    try:
        from saathimart.api.redis_fallback import get_cache_fallback
        cache = get_cache_fallback()
        
        # Get current count and window
        current = cache.get_value(cache_key) or 0
        limit = 100  # Default limit
        window = 60   # Default window
        
        # Calculate remaining and reset time
        remaining = max(0, limit - current)
        reset_time = int(time.time()) + window
        
        # Set headers
        frappe.response["X-RateLimit-Limit"] = str(limit)
        frappe.response["X-RateLimit-Remaining"] = str(remaining)
        frappe.response["X-RateLimit-Reset"] = str(reset_time)
        
        # If rate limited, add Retry-After header
        if current >= limit:
            frappe.response["Retry-After"] = str(window)
            
    except Exception:
        # Don't fail the request if rate limit headers fail
        pass


def get_request_id():
    """Get the current request's ID for logging."""
    return getattr(frappe.local, "sm_request_id", None)


def log_with_request_id(title, message, level="INFO"):
    """
    Log a message with the current request ID for correlation.
    
    Usage:
        from saathimart.api.request_tracking import log_with_request_id
        log_with_request_id("Order Created", f"Order {order_id} created for {customer}")
    """
    request_id = get_request_id()
    if request_id:
        message = f"[{request_id}] {message}"
    
    frappe.log_error(title=title, message=message)
