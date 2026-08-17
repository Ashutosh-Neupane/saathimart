import frappe
from frappe.utils import add_days, today, flt


def execute(filters=None):
    filters = filters or {}
    from_date = filters.get("from_date") or add_days(today(), -30)
    to_date = filters.get("to_date") or today()

    data = frappe.db.sql("""
        SELECT
            o.coupon_code,
            COUNT(*) as times_used,
            SUM(o.grand_total) as order_value,
            SUM(CASE WHEN o.payment_status = 'Paid' THEN o.grand_total ELSE 0 END) as paid_value,
            SUM(CASE WHEN o.payment_status = 'Paid' THEN 1 ELSE 0 END) as paid_orders,
            AVG(o.grand_total) as avg_order_value,
            SUM(o.coupon_discount) as total_discount
        FROM `tabSaathi Order` o
        WHERE DATE(o.creation) BETWEEN %s AND %s
          AND o.coupon_code != ''
        GROUP BY o.coupon_code
        ORDER BY times_used DESC
    """, (from_date, to_date), as_dict=True)

    for row in data:
        row.order_value = flt(row.order_value or 0)
        row.paid_value = flt(row.paid_value or 0)
        row.avg_order_value = flt(row.avg_order_value or 0)
        row.total_discount = flt(row.total_discount or 0)

    columns = [
        {"label": "Coupon Code", "fieldname": "coupon_code", "fieldtype": "Data", "width": 140},
        {"label": "Times Used", "fieldname": "times_used", "fieldtype": "Int", "width": 90},
        {"label": "Order Value", "fieldname": "order_value", "fieldtype": "Currency", "width": 120},
        {"label": "Paid Value", "fieldname": "paid_value", "fieldtype": "Currency", "width": 120},
        {"label": "Avg Order", "fieldname": "avg_order_value", "fieldtype": "Currency", "width": 100},
        {"label": "Total Discount", "fieldname": "total_discount", "fieldtype": "Currency", "width": 110},
    ]

    return columns, data
