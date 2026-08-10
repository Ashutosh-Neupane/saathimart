import frappe
from frappe.utils import today, add_days, flt


def execute(filters=None):
    filters = filters or {}
    from_date = filters.get("from_date") or add_days(today(), -30)
    to_date = filters.get("to_date") or today()

    conditions = ["1=1"]
    values = {}

    if filters.get("vendor"):
        conditions.append("vs.vendor = %(vendor)s")
        values["vendor"] = filters["vendor"]
    if filters.get("product"):
        conditions.append("vs.product = %(product)s")
        values["product"] = filters["product"]
    if filters.get("low_stock"):
        conditions.append("vs.available_qty < 10")

    where = "WHERE " + " AND ".join(conditions)

    data = frappe.db.sql(f"""
        SELECT
            vs.vendor,
            v.vendor_name,
            vs.product,
            p.product_name,
            p.category,
            vs.available_qty,
            vs.reserved_qty,
            vs.physical_qty,
            vs.last_known_qty,
            vs.last_updated,
            vl.price,
            vl.compare_price,
            vl.track_inventory,
            vl.allow_backorder,
            CASE
                WHEN vs.available_qty <= 0 AND vl.track_inventory = 1 THEN 'Out of Stock'
                WHEN vs.available_qty < 10 AND vl.track_inventory = 1 THEN 'Low Stock'
                WHEN vl.track_inventory = 0 THEN 'Not Tracked'
                ELSE 'In Stock'
            END as stock_status
        FROM `tabVendor Stock` vs
        LEFT JOIN `tabVendor` v ON vs.vendor = v.name
        LEFT JOIN `tabProduct` p ON vs.product = p.name
        LEFT JOIN `tabVendor Listing` vl ON vl.vendor = vs.vendor AND vl.product = vs.product AND vl.status = 'Active'
        {where}
        ORDER BY vs.available_qty ASC
        LIMIT 500
    """, values, as_dict=True)

    for row in data:
        row.available_qty = flt(row.available_qty)
        row.reserved_qty = flt(row.reserved_qty)
        row.physical_qty = flt(row.physical_qty)
        row.price = flt(row.price or 0)

    columns = [
        {"label": "Vendor", "fieldname": "vendor", "fieldtype": "Link", "options": "Vendor", "width": 120},
        {"label": "Vendor Name", "fieldname": "vendor_name", "fieldtype": "Data", "width": 160},
        {"label": "Product", "fieldname": "product", "fieldtype": "Link", "options": "Product", "width": 120},
        {"label": "Product Name", "fieldname": "product_name", "fieldtype": "Data", "width": 180},
        {"label": "Category", "fieldname": "category", "fieldtype": "Link", "options": "Category", "width": 120},
        {"label": "Available", "fieldname": "available_qty", "fieldtype": "Float", "width": 80},
        {"label": "Reserved", "fieldname": "reserved_qty", "fieldtype": "Float", "width": 80},
        {"label": "Physical", "fieldname": "physical_qty", "fieldtype": "Float", "width": 80},
        {"label": "Price", "fieldname": "price", "fieldtype": "Currency", "width": 80},
        {"label": "Status", "fieldname": "stock_status", "fieldtype": "Data", "width": 100},
        {"label": "Last Updated", "fieldname": "last_updated", "fieldtype": "Datetime", "width": 140},
    ]

    return columns, data
