import frappe
from frappe.utils import add_days, today, flt


def get_data(filters=None):
    data = frappe.db.sql("""
        SELECT o.delivery_zone, SUM(o.grand_total) as revenue
        FROM `tabOrder` o
        WHERE o.creation >= %s
        GROUP BY o.delivery_zone
        ORDER BY revenue DESC
    """, add_days(today(), -6), as_dict=True)

    labels = [str(r.delivery_zone or "Unassigned") for r in data]
    values = [flt(r.revenue) for r in data]
    return {
        "labels": labels,
        "datasets": [{"name": "Revenue (NPR)", "values": values}],
    }
