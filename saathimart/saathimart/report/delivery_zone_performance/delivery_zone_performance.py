import frappe
from frappe.utils import add_days, today, flt


def execute(filters=None):
    filters = filters or {}
    from_date = filters.get("from_date") or add_days(today(), -30)
    to_date = filters.get("to_date") or today()

    data = frappe.db.sql("""
        SELECT
            o.delivery_zone,
            COUNT(*) as orders,
            SUM(o.grand_total) as revenue,
            SUM(o.delivery_charge) as delivery_charge,
            SUM(CASE WHEN o.payment_status = 'Paid' THEN o.grand_total ELSE 0 END) as paid_revenue,
            AVG(o.grand_total) as avg_order_value,
            SUM(CASE WHEN o.status = 'Cancelled' THEN 1 ELSE 0 END) as cancelled_orders
        FROM `tabOrder` o
        WHERE DATE(o.creation) BETWEEN %s AND %s
        GROUP BY o.delivery_zone
        ORDER BY revenue DESC
    """, (from_date, to_date), as_dict=True)

    for row in data:
        row.revenue = flt(row.revenue or 0)
        row.paid_revenue = flt(row.paid_revenue or 0)
        row.avg_order_value = flt(row.avg_order_value or 0)
        row.delivery_charge = flt(row.delivery_charge or 0)

    columns = [
        {"label": "Delivery Zone", "fieldname": "delivery_zone", "fieldtype": "Link", "options": "Delivery Zone", "width": 160},
        {"label": "Orders", "fieldname": "orders", "fieldtype": "Int", "width": 80},
        {"label": "Revenue", "fieldname": "revenue", "fieldtype": "Currency", "width": 120},
        {"label": "Paid Revenue", "fieldname": "paid_revenue", "fieldtype": "Currency", "width": 120},
        {"label": "Avg Order", "fieldname": "avg_order_value", "fieldtype": "Currency", "width": 100},
        {"label": "Delivery Charge", "fieldname": "delivery_charge", "fieldtype": "Currency", "width": 120},
        {"label": "Cancelled", "fieldname": "cancelled_orders", "fieldtype": "Int", "width": 70},
    ]

    return columns, data
