import frappe
from frappe.utils import today


def get_data(filters=None):
    count = frappe.db.count("Order", {"creation": [">=", today()]})
    return {
        "value": count,
        "fieldtype": "Int",
        "label": "Today's Orders",
    }
