"""
Analytics dashboard — sales by vendor/product/location, revenue trends,
conversion rates, and business intelligence endpoints.
"""
import frappe
from frappe import _
from frappe.utils import flt, now_datetime, add_to_date, getdate


@frappe.whitelelist()
def get_dashboard_summary(days=30):
    """High-level dashboard summary for the last N days."""
    cutoff = add_to_date(now_datetime(), days=-days)

    # Order metrics
    total_orders = frappe.db.count("Order", {"creation": (">=", cutoff)})
    total_revenue = frappe.db.sql("""
        SELECT COALESCE(SUM(grand_total), 0) as total
        FROM `tabOrder` WHERE creation >= %s AND payment_status = 'Paid'
    """, (cutoff,), as_dict=True)[0].total

    # Product metrics
    total_products = frappe.db.count("Product", {"status": "Active"})
    total_vendors = frappe.db.count("Vendor", {"status": "Active"})

    # Conversion (visits → orders — approximate from API logs)
    # This is a placeholder; real conversion needs analytics middleware

    # Top products
    top_products = frappe.db.sql("""
        SELECT oi.product_name, SUM(oi.qty) as total_qty, SUM(oi.qty * oi.rate) as revenue
        FROM `tabOrder Item` oi
        INNER JOIN `tabOrder` o ON oi.parent = o.name
        WHERE o.creation >= %s AND o.payment_status = 'Paid'
        GROUP BY oi.product_name
        ORDER BY revenue DESC
        LIMIT 10
    """, (cutoff,), as_dict=True)

    # Top vendors
    top_vendors = frappe.db.sql("""
        SELECT vf.vendor, SUM(vf.subtotal) as revenue
        FROM `tabVendor Fulfillment` vf
        INNER JOIN `tabOrder` o ON vf.parent = o.name
        WHERE o.creation >= %s AND vf.status = 'Delivered'
        GROUP BY vf.vendor
        ORDER BY revenue DESC
        LIMIT 10
    """, (cutoff,), as_dict=True)

    # Revenue trend (daily)
    daily_revenue = frappe.db.sql("""
        SELECT DATE(creation) as date, SUM(grand_total) as revenue, COUNT(*) as orders
        FROM `tabOrder`
        WHERE creation >= %s AND payment_status = 'Paid'
        GROUP BY DATE(creation)
        ORDER BY date ASC
    """, (cutoff,), as_dict=True)

    return {
        "period_days": days,
        "total_orders": total_orders,
        "total_revenue": flt(total_revenue),
        "avg_order_value": flt(total_revenue / total_orders) if total_orders else 0,
        "total_products": total_products,
        "total_vendors": total_vendors,
        "top_products": top_products,
        "top_vendors": top_vendors,
        "daily_revenue": daily_revenue,
    }


@frappe.whitelelist()
def get_vendor_analytics(vendor_name, days=30):
    """Analytics for a specific vendor."""
    cutoff = add_to_date(now_datetime(), days=-days)

    orders = frappe.db.sql("""
        SELECT COUNT(*) as total,
               SUM(CASE WHEN vf.status = 'Delivered' THEN 1 ELSE 0 END) as delivered,
               SUM(CASE WHEN vf.status = 'Cancelled' THEN 1 ELSE 0 END) as cancelled,
               SUM(vf.subtotal) as revenue
        FROM `tabVendor Fulfillment` vf
        WHERE vf.vendor = %s AND vf.creation >= %s
    """, (vendor_name, cutoff), as_dict=True)[0]

    # Product performance
    products = frappe.db.sql("""
        SELECT oi.product_name, SUM(oi.qty) as qty, SUM(oi.qty * oi.rate) as revenue
        FROM `tabOrder Item` oi
        INNER JOIN `tabVendor Fulfillment` vf ON oi.parent = vf.parent
        WHERE vf.vendor = %s AND vf.creation >= %s
        GROUP BY oi.product_name
        ORDER BY revenue DESC
        LIMIT 10
    """, (vendor_name, cutoff), as_dict=True)

    return {
        "vendor": vendor_name,
        "period_days": days,
        "total_orders": orders.total or 0,
        "delivered": orders.delivered or 0,
        "cancelled": orders.cancelled or 0,
        "revenue": flt(orders.revenue or 0),
        "delivery_rate": round((orders.delivered / orders.total * 100) if orders.total else 0, 1),
        "top_products": products,
    }


@frappe.whitelelist()
def get_product_analytics(product_name, days=30):
    """Analytics for a specific product."""
    cutoff = add_to_date(now_datetime(), days=-days)

    stats = frappe.db.sql("""
        SELECT SUM(oi.qty) as total_sold, SUM(oi.qty * oi.rate) as revenue,
               COUNT(DISTINCT oi.parent) as order_count
        FROM `tabOrder Item` oi
        INNER JOIN `tabOrder` o ON oi.parent = o.name
        WHERE oi.product_name = %s AND o.creation >= %s AND o.payment_status = 'Paid'
    """, (product_name, cutoff), as_dict=True)[0]

    # Stock status across vendors
    stock = frappe.db.sql("""
        SELECT vendor, available_qty, physical_qty
        FROM `tabVendor Stock`
        WHERE product = %s
    """, (product_name,), as_dict=True)

    return {
        "product": product_name,
        "period_days": days,
        "total_sold": stats.total_sold or 0,
        "revenue": flt(stats.revenue or 0),
        "order_count": stats.order_count or 0,
        "stock_by_vendor": stock,
    }
