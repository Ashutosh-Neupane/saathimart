import frappe
from frappe.utils import today, nowdate, add_days


def execute(filters=None):
    filters = filters or {}
    from_date = filters.get("from_date") or add_days(today(), -30)
    to_date = filters.get("to_date") or today()

    data = frappe.db.sql("""
        SELECT
            DATE(creation) as date,
            COUNT(*) as orders,
            SUM(grand_total) as revenue,
            SUM(CASE WHEN payment_status = 'Paid' THEN 1 ELSE 0 END) as paid_orders,
            SUM(CASE WHEN payment_status = 'Unpaid' THEN 1 ELSE 0 END) as unpaid_orders
        FROM `tabOrder`
        WHERE DATE(creation) BETWEEN %s AND %s
        GROUP BY DATE(creation)
        ORDER BY date DESC
    """, (from_date, to_date), as_dict=True)

    columns = [
        {"label": "Date", "fieldname": "date", "fieldtype": "Date", "width": 100},
        {"label": "Orders", "fieldname": "orders", "fieldtype": "Int", "width": 80},
        {"label": "Revenue (NPR)", "fieldname": "revenue", "fieldtype": "Currency", "width": 120},
        {"label": "Paid", "fieldname": "paid_orders", "fieldtype": "Int", "width": 60},
        {"label": "Unpaid", "fieldname": "unpaid_orders", "fieldtype": "Int", "width": 60},
    ]

    return columns, data
