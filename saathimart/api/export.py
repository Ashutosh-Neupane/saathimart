"""
Data export API for admin reporting.

Provides CSV and JSON export for:
1. Orders (with filters)
2. Products (with filters)
3. Customers (with filters)
4. Vendor performance
5. Sales reports

All endpoints require SM Admin role.
"""
import csv
import io
import json
from datetime import datetime, timedelta

import frappe
from frappe import _
from frappe.utils import flt, getdate, add_days, today

from saathimart.api.responses import handle_api_errors


def _set_csv_headers(filename):
    """Set response headers for CSV download."""
    if frappe.response:
        frappe.response["Content-Type"] = "text/csv; charset=utf-8"
        frappe.response["Content-Disposition"] = f'attachment; filename="{filename}"'


def _set_json_headers(filename):
    """Set response headers for JSON download."""
    if frappe.response:
        frappe.response["Content-Type"] = "application/json; charset=utf-8"
        frappe.response["Content-Disposition"] = f'attachment; filename="{filename}"'


@frappe.whitelist()
@handle_api_errors
def export_orders(status=None, payment_status=None, vendor=None,
                  from_date=None, to_date=None, format="csv"):
    """
    Export orders with filters.
    
    Args:
        status: Filter by order status (Pending, Confirmed, etc.)
        payment_status: Filter by payment status (Paid, Unpaid, etc.)
        vendor: Filter by vendor name
        from_date: Start date (YYYY-MM-DD)
        to_date: End date (YYYY-MM-DD)
        format: Export format (csv or json)
    
    Returns:
        CSV or JSON file download
    """
    if "SM Admin" not in frappe.get_roles():
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    
    # Build filters
    filters = {}
    if status:
        filters["status"] = status
    if payment_status:
        filters["payment_status"] = payment_status
    if vendor:
        filters["vendor"] = vendor
    
    # Date range filter
    if from_date or to_date:
        filters["creation"] = ["between", [from_date or "2000-01-01", to_date or today()]]
    
    # Get orders
    orders = frappe.get_list(
        "Order",
        filters=filters,
        fields=["name", "customer_name", "customer_phone", "customer_email",
                "status", "payment_status", "payment_method", "grand_total",
                "coupon_discount", "loyalty_discount", "delivery_charge",
                "delivery_address", "delivery_zone", "vendor", "creation",
                "modified", "notes"],
        order_by="creation desc",
        limit_page_length=10000,
    )
    
    # Enrich with item count
    for order in orders:
        order["item_count"] = frappe.db.count("Order Item", {"parent": order["name"]})
    
    if format == "json":
        filename = f"orders_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        _set_json_headers(filename)
        return orders
    else:
        filename = f"orders_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        _set_csv_headers(filename)
        
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=orders[0].keys() if orders else [])
        writer.writeheader()
        writer.writerows(orders)
        
        return output.getvalue()


@frappe.whitelist()
@handle_api_errors
def export_products(category=None, brand=None, status=None, format="csv"):
    """
    Export products with filters.
    
    Args:
        category: Filter by category name
        brand: Filter by brand name
        status: Filter by status (Active, Inactive)
        format: Export format (csv or json)
    
    Returns:
        CSV or JSON file download
    """
    if "SM Admin" not in frappe.get_roles():
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    
    filters = {}
    if category:
        filters["category"] = category
    if brand:
        filters["brand"] = brand
    if status:
        filters["status"] = status
    
    products = frappe.get_list(
        "Product",
        filters=filters,
        fields=["name", "product_name", "slug", "category", "brand",
                "status", "price", "sku", "short_description", "tags",
                "avg_rating", "review_count", "has_variants", "creation"],
        order_by="creation desc",
        limit_page_length=10000,
    )
    
    if format == "json":
        filename = f"products_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        _set_json_headers(filename)
        return products
    else:
        filename = f"products_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        _set_csv_headers(filename)
        
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=products[0].keys() if products else [])
        writer.writeheader()
        writer.writerows(products)
        
        return output.getvalue()


@frappe.whitelist()
@handle_api_errors
def export_customers(from_date=None, to_date=None, format="csv"):
    """
    Export customer list with order statistics.
    
    Args:
        from_date: Start date for order stats (YYYY-MM-DD)
        to_date: End date for order stats (YYYY-MM-DD)
        format: Export format (csv or json)
    
    Returns:
        CSV or JSON file download
    """
    if "SM Admin" not in frappe.get_roles():
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    
    # Get unique customers from orders
    date_filter = ""
    params = []
    if from_date or to_date:
        date_filter = f"AND creation BETWEEN '{from_date or '2000-01-01'}' AND '{to_date or today()}'"
    
    customers = frappe.db.sql(f"""
        SELECT 
            customer_name,
            customer_phone,
            customer_email,
            COUNT(*) as total_orders,
            SUM(grand_total) as total_spent,
            MIN(creation) as first_order,
            MAX(creation) as last_order
        FROM `tabOrder`
        WHERE customer_phone IS NOT NULL AND customer_phone != ''
        {date_filter}
        GROUP BY customer_phone
        ORDER BY total_spent DESC
    """, as_dict=True)
    
    if format == "json":
        filename = f"customers_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        _set_json_headers(filename)
        return customers
    else:
        filename = f"customers_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        _set_csv_headers(filename)
        
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=customers[0].keys() if customers else [])
        writer.writeheader()
        writer.writerows(customers)
        
        return output.getvalue()


@frappe.whitelist()
@handle_api_errors
def export_vendor_performance(from_date=None, to_date=None, format="csv"):
    """
    Export vendor performance report.
    
    Args:
        from_date: Start date (YYYY-MM-DD)
        to_date: End date (YYYY-MM-DD)
        format: Export format (csv or json)
    
    Returns:
        CSV or JSON file download
    """
    if "SM Admin" not in frappe.get_roles():
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    
    date_filter = ""
    if from_date or to_date:
        date_filter = f"AND o.creation BETWEEN '{from_date or '2000-01-01'}' AND '{to_date or today()}'"
    
    performance = frappe.db.sql(f"""
        SELECT 
            v.name as vendor_id,
            v.vendor_name,
            COUNT(DISTINCT o.name) as total_orders,
            SUM(o.grand_total) as total_revenue,
            AVG(o.grand_total) as avg_order_value,
            COUNT(DISTINCT o.customer_phone) as unique_customers,
            SUM(CASE WHEN o.status = 'Delivered' THEN 1 ELSE 0 END) as delivered_orders,
            SUM(CASE WHEN o.status = 'Cancelled' THEN 1 ELSE 0 END) as cancelled_orders
        FROM `tabVendor` v
        LEFT JOIN `tabVendor Fulfillment` vf ON vf.vendor = v.name
        LEFT JOIN `tabOrder` o ON o.name = vf.parent {date_filter}
        WHERE v.status = 'Active'
        GROUP BY v.name
        ORDER BY total_revenue DESC
    """, as_dict=True)
    
    if format == "json":
        filename = f"vendor_performance_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        _set_json_headers(filename)
        return performance
    else:
        filename = f"vendor_performance_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        _set_csv_headers(filename)
        
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=performance[0].keys() if performance else [])
        writer.writeheader()
        writer.writerows(performance)
        
        return output.getvalue()


@frappe.whitelist()
@handle_api_errors
def export_sales_report(from_date=None, to_date=None, group_by="day", format="csv"):
    """
    Export sales report grouped by day/week/month.
    
    Args:
        from_date: Start date (YYYY-MM-DD)
        to_date: End date (YYYY-MM-DD)
        group_by: Grouping (day, week, month)
        format: Export format (csv or json)
    
    Returns:
        CSV or JSON file download
    """
    if "SM Admin" not in frappe.get_roles():
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    
    date_filter = ""
    if from_date or to_date:
        date_filter = f"WHERE creation BETWEEN '{from_date or '2000-01-01'}' AND '{to_date or today()}'"
    
    if group_by == "month":
        group_expr = "DATE_FORMAT(creation, '%Y-%m-01')"
    elif group_by == "week":
        group_expr = "DATE(DATE_SUB(creation, INTERVAL WEEKDAY(creation) DAY))"
    else:
        group_expr = "DATE(creation)"
    
    sales = frappe.db.sql(f"""
        SELECT 
            {group_expr} as date,
            COUNT(*) as orders,
            SUM(grand_total) as revenue,
            AVG(grand_total) as avg_order_value,
            SUM(coupon_discount) as total_discounts,
            SUM(delivery_charge) as total_delivery_charges
        FROM `tabOrder`
        {date_filter}
        GROUP BY {group_expr}
        ORDER BY date DESC
    """, as_dict=True)
    
    if format == "json":
        filename = f"sales_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        _set_json_headers(filename)
        return sales
    else:
        filename = f"sales_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        _set_csv_headers(filename)
        
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=sales[0].keys() if sales else [])
        writer.writeheader()
        writer.writerows(sales)
        
        return output.getvalue()
