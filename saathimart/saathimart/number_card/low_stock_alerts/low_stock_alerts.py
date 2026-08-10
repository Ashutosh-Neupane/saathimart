import frappe
from frappe.utils import today


def get_data(filters=None):
    count = frappe.db.sql("""
        SELECT COUNT(*) FROM `tabVendor Stock` vs
        LEFT JOIN `tabVendor Listing` vl ON vl.vendor = vs.vendor AND vl.product = vs.product AND vl.status = 'Active'
        WHERE vl.track_inventory = 1 AND vs.available_qty < 10
    """)[0][0]
    return {
        "value": count or 0,
        "fieldtype": "Int",
        "label": "Low Stock Alerts",
    }
