import frappe
from frappe.utils import today


def get_data(filters=None):
    count = frappe.db.count("Order", {
        "creation": [">=", today()],
        "status": ["in", ["Pending", "Processing"]]
    })
    return {
        "value": count,
        "fieldtype": "Int",
        "label": "Pending Orders",
    }
