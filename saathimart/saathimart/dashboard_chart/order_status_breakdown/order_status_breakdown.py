import frappe
from frappe.utils import add_days, today


def get_data(filters=None):
    data = frappe.db.sql("""
        SELECT status, COUNT(*) as orders
        FROM `tabOrder`
        WHERE creation >= %s
        GROUP BY status
        ORDER BY orders DESC
    """, add_days(today(), -6), as_dict=True)

    labels = [str(r.status or "Unknown") for r in data]
    values = [r.orders for r in data]
    return {
        "labels": labels,
        "datasets": [{"name": "Orders", "values": values}],
    }
