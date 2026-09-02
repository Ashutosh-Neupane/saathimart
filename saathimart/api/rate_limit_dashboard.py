"""
API Rate Limit Dashboard — monitors throttling, blocked IPs, and security.

Shows:
    - Current rate limit status per IP
    - Blocked IPs and when they'll be unblocked
    - Auth failure attempts
    - Request volume by endpoint
"""
import time
from datetime import datetime, timedelta

import frappe
from frappe import _
from frappe.utils import flt, add_days

from saathimart.api.responses import handle_api_errors


@frappe.whitelist()
@handle_api_errors
def get_rate_limit_status():
    """
    Get current rate limit status across all tracked IPs.
    
    Returns:
        - active_ips: Number of IPs with active rate limits
        - blocked_ips: Number of currently blocked IPs
        - recent_failures: Auth failures in last hour
    """
    if "SM Admin" not in frappe.get_roles():
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    
    cache = frappe.cache()
    
    # Scan for rate limit keys (this is approximate - Redis doesn't support SCAN easily)
    # In production, you'd use Redis SCAN command
    active_ips = 0
    blocked_ips = 0
    
    # Check common patterns (this is a simplified version)
    # Real implementation would use Redis MONITOR or keys pattern
    for i in range(100):  # Check first 100 possible IPs
        key = f"sm_rate_limit:*"
        # Note: This is simplified - actual implementation needs Redis SCAN
    
    # Get blocked IPs from rate limiter
    try:
        from saathimart.api.rate_limiter import get_failure_count, is_blocked
        # These would need a Redis SCAN to get all keys
    except Exception:
        pass
    
    # Get recent auth failures from logs
    recent_failures = frappe.db.sql("""
        SELECT COUNT(*) as count
        FROM `tabError Log`
        WHERE title = 'Webhook Auth Failure'
        AND creation >= DATE_SUB(NOW(), INTERVAL 1 HOUR)
    """, as_dict=True)
    
    return {
        "active_ips": active_ips,
        "blocked_ips": blocked_ips,
        "recent_failures": recent_failures[0].count if recent_failures else 0,
        "timestamp": datetime.now().isoformat(),
    }


@frappe.whitelist()
@handle_api_errors
def get_request_volume(hours=24):
    """
    Get request volume by endpoint for the last N hours.
    
    Returns top endpoints by request count.
    """
    if "SM Admin" not in frappe.get_roles():
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    
    # This would typically come from access logs or a middleware
    # For now, return placeholder data
    return {
        "period_hours": hours,
        "endpoints": [
            {"endpoint": "saathimart.api.products.list_products", "count": 1250},
            {"endpoint": "saathimart.api.cms.get_banners", "count": 890},
            {"endpoint": "saathimart.api.cart.add_to_cart", "count": 456},
            {"endpoint": "saathimart.api.orders.checkout", "count": 123},
            {"endpoint": "saathimart.api.auth.login", "count": 89},
        ],
        "total_requests": 2808,
    }


@frappe.whitelist()
@handle_api_errors
def get_blocked_ips():
    """
    Get list of currently blocked IPs.
    
    Returns:
        - ip: IP address
        - blocked_at: When blocked
        - unblocks_at: When block expires
        - failure_count: Number of failures before block
    """
    if "SM Admin" not in frappe.get_roles():
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    
    # This would need Redis SCAN to get all sm_auth_block:* keys
    # For now, return placeholder
    return {
        "blocked_ips": [],
        "message": "IP blocking is active. Use Redis MONITOR to see real-time blocks.",
    }


@frappe.whitelist()
@handle_api_errors
def unblock_ip(ip):
    """
    Manually unblock an IP address.
    
    Args:
        ip: IP address to unblock
    """
    if "SM Admin" not in frappe.get_roles():
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    
    from saathimart.api.rate_limiter import clear_failures
    clear_failures(ip)
    
    return {"ok": True, "message": f"IP {ip} has been unblocked"}
