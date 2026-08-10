import frappe
from frappe.utils import add_days, today, flt


def execute(filters=None):
    filters = filters or {}
    from_date = filters.get("from_date") or add_days(today(), -30)
    to_date = filters.get("to_date") or today()

    conditions = ["DATE(o.creation) BETWEEN %(from_date)s AND %(to_date)s"]
    values = {"from_date": from_date, "to_date": to_date}

    if filters.get("vendor"):
        conditions.append("o.vendor = %(vendor)s")
        values["vendor"] = filters["vendor"]

    where = "WHERE " + " AND ".join(conditions)

    data = frappe.db.sql(f"""
        SELECT
            o.vendor,
            v.vendor_name,
            COUNT(*) as order_count,
            SUM(o.grand_total) as revenue,
            SUM(CASE WHEN o.payment_status = 'Paid' THEN 1 ELSE 0 END) as paid_orders,
            SUM(CASE WHEN o.payment_status = 'Unpaid' THEN 1 ELSE 0 END) as unpaid_orders,
            SUM(CASE WHEN o.status = 'Cancelled' THEN 1 ELSE 0 END) as cancelled_orders,
            AVG(o.grand_total) as avg_order_value,
            SUM(o.delivery_charge) as total_delivery_charge
        FROM `tabOrder` o
        LEFT JOIN `tabVendor` v ON o.vendor = v.name
        {where}
        GROUP BY o.vendor
        ORDER BY revenue DESC
    """, values, as_dict=True)

    for row in data:
        row.fulfillment_rate = flt(row.order_count - (row.cancelled_orders or 0)) / flt(row.order_count) * 100 if row.order_count else 0
        row.revenue = flt(row.revenue or 0)
        row.avg_order_value = flt(row.avg_order_value or 0)

    columns = [
        {"label": "Vendor", "fieldname": "vendor", "fieldtype": "Link", "options": "Vendor", "width": 150},
        {"label": "Name", "fieldname": "vendor_name", "fieldtype": "Data", "width": 200},
        {"label": "Orders", "fieldname": "order_count", "fieldtype": "Int", "width": 80},
        {"label": "Revenue", "fieldname": "revenue", "fieldtype": "Currency", "width": 120},
        {"label": "Avg Order", "fieldname": "avg_order_value", "fieldtype": "Currency", "width": 100},
        {"label": "Paid", "fieldname": "paid_orders", "fieldtype": "Int", "width": 60},
        {"label": "Unpaid", "fieldname": "unpaid_orders", "fieldtype": "Int", "width": 60},
        {"label": "Cancelled", "fieldname": "cancelled_orders", "fieldtype": "Int", "width": 70},
        {"label": "Fulfillment %", "fieldname": "fulfillment_rate", "fieldtype": "Percent", "width": 100},
        {"label": "Delivery Charge", "fieldname": "total_delivery_charge", "fieldtype": "Currency", "width": 120},
    ]

    return columns, data
