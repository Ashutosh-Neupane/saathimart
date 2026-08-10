import frappe
from frappe.utils import today


def get_data(filters=None):
    result = frappe.db.get_value(
        "Order",
        {"creation": [">=", today()], "payment_status": "Paid"},
        "SUM(grand_total)",
    )
    return {
        "value": result or 0,
        "fieldtype": "Currency",
        "label": "Today's Revenue",
    }
