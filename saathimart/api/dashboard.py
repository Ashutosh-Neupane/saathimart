"""
Frappe Desk Dashboard — visualizes sync health, vendor performance,
and order analytics in one view.

All endpoints require SM Admin role. Data is cached for 5 minutes
to avoid hammering the database on every dashboard refresh.
"""
import json
from datetime import datetime, timedelta

import frappe
from frappe import _
from frappe.utils import flt, today, add_days, getdate

from saathimart.api.responses import handle_api_errors


def _get_cache(key, ttl=300):
    """Get cached value or None."""
    return frappe.cache().get_value(key)


def _set_cache(key, value, ttl=300):
    """Set cached value."""
    frappe.cache().set_value(key, value, expires_in_sec=ttl)


@frappe.whitelist()
@handle_api_errors
def get_dashboard_summary():
    """
    Main dashboard summary — all key metrics in one call.
    
    Returns:
        - orders_today: Orders placed today
        - revenue_today: Revenue today (NPR)
        - active_vendors: Number of active vendors
        - pending_orders: Orders waiting for fulfillment
        - delivered_today: Orders delivered today
        - conversion_rate: Checkout success rate
    """
    if "SM Admin" not in frappe.get_roles():
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    
    cache_key = "sm_dashboard_summary"
    cached = _get_cache(cache_key)
    if cached:
        return cached
    
    today_str = today()
    
    # Orders today
    orders_today = frappe.db.count("Order", {
        "creation": ["like", f"{today_str}%"],
    })
    
    # Revenue today
    revenue = frappe.db.sql("""
        SELECT COALESCE(SUM(grand_total), 0) as total
        FROM `tabOrder`
        WHERE creation LIKE %s AND status != 'Cancelled'
    """, (f"{today_str}%",), as_dict=True)
    revenue_today = flt(revenue[0].total) if revenue else 0
    
    # Active vendors
    active_vendors = frappe.db.count("Vendor", {"status": "Active"})
    
    # Pending orders (awaiting fulfillment)
    pending_orders = frappe.db.count("Order", {
        "status": ["in", ["Pending", "Confirmed"]],
        "payment_status": "Paid",
    })
    
    # Delivered today
    delivered_today = frappe.db.count("Order", {
        "status": "Delivered",
        "modified": ["like", f"{today_str}%"],
    })
    
    # Conversion rate (orders / carts created today)
    carts_today = frappe.db.count("Cart", {
        "creation": ["like", f"{today_str}%"],
    })
    conversion_rate = round((orders_today / carts_today * 100), 1) if carts_today > 0 else 0
    
    # Average order value
    avg_order = frappe.db.sql("""
        SELECT COALESCE(AVG(grand_total), 0) as avg_val
        FROM `tabOrder`
        WHERE creation LIKE %s AND status != 'Cancelled'
    """, (f"{today_str}%",), as_dict=True)
    avg_order_value = flt(avg_order[0].avg_val) if avg_order else 0
    
    result = {
        "orders_today": orders_today,
        "revenue_today": revenue_today,
        "active_vendors": active_vendors,
        "pending_orders": pending_orders,
        "delivered_today": delivered_today,
        "conversion_rate": conversion_rate,
        "avg_order_value": avg_order_value,
        "carts_today": carts_today,
    }
    
    _set_cache(cache_key, result, ttl=300)
    return result


@frappe.whitelist()
@handle_api_errors
def get_order_trends(days=7):
    """
    Order trends for the last N days.
    
    Returns daily breakdown of:
        - orders: Count of orders
        - revenue: Total revenue (NPR)
        - avg_order_value: Average order value
    """
    if "SM Admin" not in frappe.get_roles():
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    
    cache_key = f"sm_order_trends_{days}"
    cached = _get_cache(cache_key)
    if cached:
        return cached
    
    days = min(90, max(1, int(days)))
    start_date = add_days(today(), -days)
    
    trends = frappe.db.sql("""
        SELECT 
            DATE(creation) as date,
            COUNT(*) as orders,
            COALESCE(SUM(grand_total), 0) as revenue,
            COALESCE(AVG(grand_total), 0) as avg_order_value
        FROM `tabOrder`
        WHERE creation >= %s AND status != 'Cancelled'
        GROUP BY DATE(creation)
        ORDER BY date ASC
    """, (start_date,), as_dict=True)
    
    # Fill in missing dates with zeros
    result = []
    date_map = {str(t.date): t for t in trends}
    
    for i in range(days):
        d = add_days(today(), -i)
        date_str = str(d)
        if date_str in date_map:
            t = date_map[date_str]
            result.append({
                "date": date_str,
                "orders": t.orders,
                "revenue": flt(t.revenue),
                "avg_order_value": flt(t.avg_order_value),
            })
        else:
            result.append({
                "date": date_str,
                "orders": 0,
                "revenue": 0,
                "avg_order_value": 0,
            })
    
    result.reverse()  # Oldest first
    _set_cache(cache_key, result, ttl=300)
    return result


@frappe.whitelist()
@handle_api_errors
def get_vendor_performance():
    """
    Vendor performance metrics.
    
    Returns for each active vendor:
        - total_orders: Orders fulfilled
        - total_revenue: Revenue generated
        - avg_delivery_time: Average delivery time (minutes)
        - fulfillment_rate: % of orders completed successfully
        - rating: Average customer rating
    """
    if "SM Admin" not in frappe.get_roles():
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    
    cache_key = "sm_vendor_performance"
    cached = _get_cache(cache_key)
    if cached:
        return cached
    
    performance = frappe.db.sql("""
        SELECT 
            v.name as vendor_id,
            v.vendor_name,
            COUNT(DISTINCT o.name) as total_orders,
            COALESCE(SUM(o.grand_total), 0) as total_revenue,
            COALESCE(AVG(o.grand_total), 0) as avg_order_value,
            SUM(CASE WHEN o.status = 'Delivered' THEN 1 ELSE 0 END) as delivered,
            SUM(CASE WHEN o.status = 'Cancelled' THEN 1 ELSE 0 END) as cancelled,
            SUM(CASE WHEN o.status IN ('Pending', 'Confirmed') THEN 1 ELSE 0 END) as pending
        FROM `tabVendor` v
        LEFT JOIN `tabVendor Fulfillment` vf ON vf.vendor = v.name
        LEFT JOIN `tabOrder` o ON o.name = vf.parent
        WHERE v.status = 'Active'
        GROUP BY v.name
        ORDER BY total_revenue DESC
    """, as_dict=True)
    
    # Calculate fulfillment rate
    for p in performance:
        total = (p.delivered or 0) + (p.cancelled or 0)
        p.fulfillment_rate = round((p.delivered / total * 100), 1) if total > 0 else 0
        p.total_revenue = flt(p.total_revenue)
        p.avg_order_value = flt(p.avg_order_value)
    
    _set_cache(cache_key, performance, ttl=300)
    return performance


@frappe.whitelist()
@handle_api_errors
def get_sync_health():
    """
    Webhook sync health — shows delivery status of events to vendors.
    
    Returns:
        - total_events: Total events in last 24h
        - delivered: Successfully delivered
        - pending: Awaiting delivery
        - failed: Failed deliveries
        - dead: Dead-lettered events
    """
    if "SM Admin" not in frappe.get_roles():
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    
    cache_key = "sm_sync_health"
    cached = _get_cache(cache_key)
    if cached:
        return cached
    
    yesterday = add_days(today(), -1)
    
    # Event counts by status
    stats = frappe.db.sql("""
        SELECT 
            delivery_status,
            COUNT(*) as count
        FROM `tabWebhook Event`
        WHERE creation >= %s
        GROUP BY delivery_status
    """, (yesterday,), as_dict=True)
    
    status_map = {s.delivery_status: s.count for s in stats}
    
    result = {
        "total_events": sum(status_map.values()),
        "delivered": status_map.get("Delivered", 0),
        "pending": status_map.get("Pending", 0),
        "failed": status_map.get("Failed", 0),
        "dead": status_map.get("Dead", 0),
        "delivery_rate": 0,
    }
    
    if result["total_events"] > 0:
        result["delivery_rate"] = round(
            (result["delivered"] / result["total_events"]) * 100, 1
        )
    
    _set_cache(cache_key, result, ttl=300)
    return result


@frappe.whitelist()
@handle_api_errors
def get_top_products(limit=10):
    """
    Top selling products by order count.
    
    Returns product name, total orders, and total revenue.
    """
    if "SM Admin" not in frappe.get_roles():
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    
    cache_key = f"sm_top_products_{limit}"
    cached = _get_cache(cache_key)
    if cached:
        return cached
    
    products = frappe.db.sql("""
        SELECT 
            oi.product_name,
            oi.product,
            COUNT(DISTINCT oi.parent) as order_count,
            SUM(oi.amount) as total_revenue
        FROM `tabOrder Item` oi
        INNER JOIN `tabOrder` o ON o.name = oi.parent
        WHERE o.status != 'Cancelled'
        GROUP BY oi.product
        ORDER BY order_count DESC
        LIMIT %s
    """, (limit,), as_dict=True)
    
    result = []
    for p in products:
        result.append({
            "product_name": p.product_name,
            "product": p.product,
            "order_count": p.order_count,
            "total_revenue": flt(p.total_revenue),
        })
    
    _set_cache(cache_key, result, ttl=300)
    return result


@frappe.whitelist()
@handle_api_errors
def get_payment_summary():
    """
    Payment status breakdown.
    
    Returns:
        - paid: Orders with Paid status
        - unpaid: Orders with Unpaid status
        - cod: Orders with COD payment
        - esewa: Orders with eSewa payment
        - total_revenue: Total revenue by payment method
    """
    if "SM Admin" not in frappe.get_roles():
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    
    cache_key = "sm_payment_summary"
    cached = _get_cache(cache_key)
    if cached:
        return cached
    
    # Payment status counts
    status_counts = frappe.db.sql("""
        SELECT 
            payment_status,
            COUNT(*) as count,
            COALESCE(SUM(grand_total), 0) as total
        FROM `tabOrder`
        WHERE status != 'Cancelled'
        GROUP BY payment_status
    """, as_dict=True)
    
    # Payment method breakdown
    method_counts = frappe.db.sql("""
        SELECT 
            payment_method,
            COUNT(*) as count,
            COALESCE(SUM(grand_total), 0) as total
        FROM `tabOrder`
        WHERE status != 'Cancelled'
        GROUP BY payment_method
    """, as_dict=True)
    
    result = {
        "by_status": {s.payment_status: {"count": s.count, "total": flt(s.total)} for s in status_counts},
        "by_method": {m.payment_method: {"count": m.count, "total": flt(m.total)} for m in method_counts},
    }
    
    _set_cache(cache_key, result, ttl=300)
    return result
