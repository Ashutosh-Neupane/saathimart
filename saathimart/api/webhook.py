"""
Webhook system for Next.js ISR (Incremental Static Regeneration).

When content changes in Frappe (products, orders, CMS), this system
notifies the Next.js frontend to revalidate its static pages.

Usage:
    POST /api/method/saathimart.api.webhook.revalidate_nextjs
    {
        "paths": ["/products/rice-123", "/orders/SM-ORD-2026-00001"],
        "tag": "product"  # optional: coarse area ("product"|"order"|"cms")
    }

    POST /api/method/saathimart.api.webhook.revalidate_all_products
    POST /api/method/saathimart.api.webhook.revalidate_all_orders

Delivery (URL, secret header, tags body) lives in
saathimart.api.storefront_cache — this module is the manual/admin surface
on top of it.
"""
import frappe
from frappe import _

from saathimart.api.responses import handle_api_errors
from saathimart.api.utils import safe_enqueue


def _call_nextjs_revalidate(paths, tag=None):
    """
    Call Next.js ISR revalidation endpoint.
    
    Delegates to saathimart.api.storefront_cache.notify_nextjs, which speaks
    the route's real contract: `x-revalidate-secret` header + `tags` array
    body (the old `{secret, paths, tag}` body shape was never accepted by
    the storefront's app/api/revalidate/route.ts). Path-based revalidation
    maps onto the tag universe; a specific product name becomes its
    catalog-product-{slug} tag when resolvable.
    
    Args:
        paths: List of paths to revalidate (e.g., ["/products/rice"])
        tag: Optional legacy coarse tag ("product", "order", "cms")
    """
    from saathimart.api.storefront_cache import (
        cms_tags, notify_nextjs, product_tags,
    )

    tags = []
    if tag in ("product", None):
        for p in paths or []:
            slug = p.rstrip("/").rsplit("/", 1)[-1] if p else ""
            product = frappe.db.get_value("Product", {"slug": slug}, "name") if slug else None
            tags.extend(product_tags(product))
        if not paths:
            tags.append("catalog-list")
    if tag in ("order", None):
        tags.extend(f"orders-detail-{p.rstrip('/').rsplit('/', 1)[-1]}" for p in paths or [] if "/orders/" in p)
    if tag in ("cms", None) or not tags:
        tags.extend(cms_tags())

    return notify_nextjs(tags, remark="webhook.py manual revalidate")


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
