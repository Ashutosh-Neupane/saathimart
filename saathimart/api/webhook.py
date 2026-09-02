"""
Webhook system for Next.js ISR (Incremental Static Regeneration).

When content changes in Frappe (products, orders, CMS), this system
notifies the Next.js frontend to revalidate its static pages.

Usage:
    POST /api/method/saathimart.api.webhook.revalidate_nextjs
    {
        "paths": ["/products/rice-123", "/orders/SM-ORD-2026-00001"],
        "tag": "product"  # optional: for tag-based revalidation
    }

    POST /api/method/saathimart.api.webhook.revalidate_all_products
    POST /api/method/saathimart.api.webhook.revalidate_all_orders
"""
import json

import frappe
from frappe import _

from saathimart.api.responses import handle_api_errors
from saathimart.api.utils import safe_enqueue


# Next.js ISR revalidation secret (should match NEXTJS_ISR_SECRET env var)
NEXTJS_REVALIDATE_SECRET = None


def _get_revalidate_secret():
    """Get the ISR revalidation secret from site config."""
    global NEXTJS_REVALIDATE_SECRET
    if NEXTJS_REVALIDATE_SECRET is None:
        NEXTJS_REVALIDATE_SECRET = frappe.conf.get("nextjs_revalidate_secret", "")
    return NEXTJS_REVALIDATE_SECRET


def _call_nextjs_revalidate(paths, tag=None):
    """
    Call Next.js ISR revalidation endpoint.
    
    Args:
        paths: List of paths to revalidate (e.g., ["/products/rice"])
        tag: Optional tag for tag-based revalidation
    """
    import requests
    
    secret = _get_revalidate_secret()
    if not secret:
        frappe.log_error("Next.js ISR secret not configured", "Webhook Config Error")
        return False
    
    # Next.js API route for revalidation
    nextjs_url = frappe.conf.get("nextjs_url", "http://localhost:3000")
    revalidate_endpoint = f"{nextjs_url}/api/revalidate"
    
    try:
        response = requests.post(
            revalidate_endpoint,
            json={
                "secret": secret,
                "paths": paths,
                "tag": tag,
            },
            timeout=5,  # Don't block on slow Next.js
            headers={"Content-Type": "application/json"},
        )
        
        if response.status_code == 200:
            return True
        else:
            frappe.log_error(
                f"Next.js revalidation failed: {response.status_code} {response.text}",
                "Webhook Error"
            )
            return False
            
    except requests.exceptions.RequestException as e:
        frappe.log_error(f"Next.js revalidation request failed: {str(e)}", "Webhook Error")
        return False


def _notify_product_change(product_name, action="update"):
    """
    Notify Next.js about product changes.
    
    Args:
        product_name: Product name
        action: "create", "update", or "delete"
    """
    paths = [
        f"/products/{product_name}",
        "/products",  # Product listing
        "/",  # Homepage
    ]
    
    safe_enqueue(
        _call_nextjs_revalidate,
        paths=paths,
        tag="product",
        queue="short",
    )


def _notify_order_change(order_id, action="update"):
    """
    Notify Next.js about order changes.
    
    Args:
        order_id: Order ID
        action: "create", "update", or "delete"
    """
    paths = [
        f"/orders/{order_id}",
        "/orders",  # Order listing
    ]
    
    safe_enqueue(
        _call_nextjs_revalidate,
        paths=paths,
        tag="order",
        queue="short",
    )


def _notify_cms_change(content_type, content_name):
    """
    Notify Next.js about CMS content changes.
    
    Args:
        content_type: "banner", "faq", "offer", etc.
        content_name: Content name
    """
    paths = [
        "/",  # Homepage
        f"/{content_type}s",  # Content listing
    ]
    
    safe_enqueue(
        _call_nextjs_revalidate,
        paths=paths,
        tag=content_type,
        queue="short",
    )


@frappe.whitelist()
@handle_api_errors
def revalidate_nextjs(paths=None, tag=None):
    """
    Manually trigger Next.js ISR revalidation.
    
    Args:
        paths: List of paths to revalidate
        tag: Optional tag for tag-based revalidation
    """
    if "SM Admin" not in frappe.get_roles():
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    
    if not paths and not tag:
        frappe.throw(_("Either paths or tag is required"))
    
    result = _call_nextjs_revalidate(paths or [], tag)
    return {"ok": result, "paths": paths, "tag": tag}


@frappe.whitelist()
@handle_api_errors
def revalidate_all_products():
    """
    Revalidate all product pages in Next.js.
    Should be called after bulk product updates.
    """
    if "SM Admin" not in frappe.get_roles():
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    
    # Get all product names
    products = frappe.get_all("Product", pluck="name", limit_page_length=10000)
    paths = [f"/products/{p}" for p in products] + ["/products", "/"]
    
    safe_enqueue(
        _call_nextjs_revalidate,
        paths=paths,
        tag="product",
        queue="short",
    )
    
    return {"ok": True, "products_count": len(products)}


@frappe.whitelist()
@handle_api_errors
def revalidate_all_orders():
    """
    Revalidate all order pages in Next.js.
    Should be called after bulk order updates.
    """
    if "SM Admin" not in frappe.get_roles():
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    
    # Get all order names
    orders = frappe.get_all("Order", pluck="name", limit_page_length=10000)
    paths = [f"/orders/{o}" for o in orders] + ["/orders"]
    
    safe_enqueue(
        _call_nextjs_revalidate,
        paths=paths,
        tag="order",
        queue="short",
    )
    
    return {"ok": True, "orders_count": len(orders)}


@frappe.whitelist()
@handle_api_errors
def revalidate_homepage():
    """
    Revalidate the Next.js homepage.
    Should be called after CMS changes.
    """
    if "SM Admin" not in frappe.get_roles():
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    
    safe_enqueue(
        _call_nextjs_revalidate,
        paths=["/"],
        tag="homepage",
        queue="short",
    )
    
    return {"ok": True}
