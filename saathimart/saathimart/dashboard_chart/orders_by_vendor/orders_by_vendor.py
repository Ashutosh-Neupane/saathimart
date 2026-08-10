import frappe
from frappe.utils import add_days, today


def get_data(filters=None):
    data = frappe.db.sql("""
        SELECT vendor, COUNT(*) as orders
        FROM `tabOrder`
        WHERE creation >= %s
        GROUP BY vendor
        ORDER BY orders DESC
        LIMIT 10
    """, add_days(today(), -6), as_dict=True)

    labels = [str(r.vendor or "Unassigned") for r in data]
    values = [r.orders for r in data]
    return {
        "labels": labels,
        "datasets": [{"name": "Orders", "values": values}],
    }
