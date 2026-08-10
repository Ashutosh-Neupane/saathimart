import frappe
from frappe.utils import today


def get_data(filters=None):
    count = frappe.db.count("Vendor", {"status": "Active"})
    return {
        "value": count,
        "fieldtype": "Int",
        "label": "Active Vendors",
    }
