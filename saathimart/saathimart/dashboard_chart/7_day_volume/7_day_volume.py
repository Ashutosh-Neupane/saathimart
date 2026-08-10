import frappe
from frappe.utils import add_days, today


def get_data(filters=None):
    data = frappe.db.sql("""
        SELECT DATE(creation) as date, COUNT(*) as orders
        FROM `tabOrder`
        WHERE creation >= %s
        GROUP BY DATE(creation)
        ORDER BY date ASC
    """, add_days(today(), -6), as_dict=True)

    labels = [str(r.date) for r in data]
    values = [r.orders for r in data]
    return {
        "labels": labels,
        "datasets": [{"name": "Orders", "values": values}],
    }
