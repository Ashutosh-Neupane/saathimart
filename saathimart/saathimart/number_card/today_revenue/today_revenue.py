import frappe
from frappe.utils import today


def get_data(filters=None):
    # Aggregates must use string-function syntax here ("sum(col) as alias");
    # dict syntax ({"SUM": "grand_total"}) is parsed as a child-table spec
    # by this Frappe version's query engine and crashes.
    result = frappe.db.get_value(
        "Order",
        {"creation": [">=", today()], "payment_status": "Paid"},
        "sum(grand_total) as total",
        as_dict=True,
    )
    return {
        "value": result.total if result and result.total else 0,
        "fieldtype": "Currency",
        "label": "Today's Revenue",
    }
