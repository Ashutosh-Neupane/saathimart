import frappe
from frappe.utils import add_days, today, flt


def execute(filters=None):
    filters = filters or {}
    from_date = filters.get("from_date") or add_days(today(), -30)
    to_date = filters.get("to_date") or today()

    data = frappe.db.sql("""
        SELECT
            payment_method,
            COUNT(*) as orders,
            SUM(grand_total) as revenue,
            SUM(CASE WHEN payment_status = 'Paid' THEN grand_total ELSE 0 END) as paid_revenue,
            SUM(CASE WHEN payment_status = 'Pending' THEN grand_total ELSE 0 END) as pending_revenue,
            SUM(CASE WHEN payment_status = 'Failed' THEN grand_total ELSE 0 END) as failed_revenue
        FROM `tabOrder`
        WHERE DATE(creation) BETWEEN %s AND %s
        GROUP BY payment_method
        ORDER BY revenue DESC
    """, (from_date, to_date), as_dict=True)

    for row in data:
        row.revenue = flt(row.revenue or 0)
        row.paid_revenue = flt(row.paid_revenue or 0)
        row.pending_revenue = flt(row.pending_revenue or 0)
        row.failed_revenue = flt(row.failed_revenue or 0)

    columns = [
        {"label": "Payment Method", "fieldname": "payment_method", "fieldtype": "Data", "width": 140},
        {"label": "Orders", "fieldname": "orders", "fieldtype": "Int", "width": 80},
        {"label": "Revenue", "fieldname": "revenue", "fieldtype": "Currency", "width": 120},
        {"label": "Paid", "fieldname": "paid_revenue", "fieldtype": "Currency", "width": 110},
        {"label": "Pending", "fieldname": "pending_revenue", "fieldtype": "Currency", "width": 110},
        {"label": "Failed", "fieldname": "failed_revenue", "fieldtype": "Currency", "width": 100},
    ]

    return columns, data
