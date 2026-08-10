import frappe
from frappe.utils import add_days, today


def execute(filters=None):
    filters = filters or {}
    from_date = filters.get("from_date") or add_days(today(), -30)
    to_date = filters.get("to_date") or today()

    data = frappe.db.sql("""
        SELECT
            oi.product as product_code,
            p.product_name,
            SUM(oi.qty) as total_qty,
            SUM(oi.amount) as total_revenue,
            COUNT(DISTINCT oi.parent) as order_count
        FROM `tabOrder Item` oi
        JOIN `tabOrder` o ON oi.parent = o.name
        LEFT JOIN `tabProduct` p ON oi.product = p.name
        WHERE DATE(o.creation) BETWEEN %s AND %s
        GROUP BY oi.product
        ORDER BY total_qty DESC
        LIMIT 20
    """, (from_date, to_date), as_dict=True)

    columns = [
        {"label": "Product", "fieldname": "product_code", "fieldtype": "Link", "options": "Product", "width": 150},
        {"label": "Name", "fieldname": "product_name", "fieldtype": "Data", "width": 200},
        {"label": "Qty Sold", "fieldname": "total_qty", "fieldtype": "Float", "width": 80},
        {"label": "Revenue", "fieldname": "total_revenue", "fieldtype": "Currency", "width": 120},
        {"label": "Orders", "fieldname": "order_count", "fieldtype": "Int", "width": 70},
    ]

    return columns, data
