import frappe
from frappe.utils import getdate


def execute(filters=None):
    filters = filters or {}
    return get_columns(), get_data(filters)


def get_columns():
    return [
        {"label": "Date",         "fieldname": "posting_date",  "fieldtype": "Date",    "width": 100},
        {"label": "Time",         "fieldname": "posting_time",  "fieldtype": "Time",    "width": 80},
        {"label": "Product",      "fieldname": "product",       "fieldtype": "Link",    "options": "Product", "width": 180},
        {"label": "Voucher Type", "fieldname": "voucher_type",  "fieldtype": "Data",    "width": 140},
        {"label": "Voucher No",   "fieldname": "voucher_no",    "fieldtype": "Data",    "width": 160},
        {"label": "Qty Change",   "fieldname": "qty_change",    "fieldtype": "Float",   "width": 100},
        {"label": "Balance Qty",  "fieldname": "balance_qty",   "fieldtype": "Float",   "width": 110},
        {"label": "Source Site",  "fieldname": "source_site",   "fieldtype": "Data",    "width": 140},
        {"label": "Vendor",       "fieldname": "vendor",        "fieldtype": "Link",    "options": "Vendor", "width": 130},
        {"label": "Remarks",      "fieldname": "remarks",       "fieldtype": "Data",    "width": 200},
    ]


def get_data(filters):
    conditions = []
    values = {}

    if filters.get("from_date"):
        conditions.append("posting_date >= %(from_date)s")
        values["from_date"] = filters["from_date"]
    if filters.get("to_date"):
        conditions.append("posting_date <= %(to_date)s")
        values["to_date"] = filters["to_date"]
    if filters.get("product"):
        conditions.append("product = %(product)s")
        values["product"] = filters["product"]
    if filters.get("voucher_type"):
        conditions.append("voucher_type = %(voucher_type)s")
        values["voucher_type"] = filters["voucher_type"]

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    return frappe.db.sql(f"""
        SELECT
            posting_date, posting_time, product, voucher_type,
            voucher_no, qty_change, balance_qty, source_site, vendor, remarks
        FROM `tabStock Ledger Entry`
        {where}
        ORDER BY posting_date DESC, posting_time DESC
    """, values, as_dict=True)
