import frappe
from frappe.utils import flt


def execute(filters=None):
    filters = filters or {}
    vendor_filter = filters.get("vendor")
    zone_filter = filters.get("delivery_zone")

    conditions = ["p.status = 'Active'"]
    values = {}

    if vendor_filter:
        conditions.append("vl.vendor = %(vendor)s")
        values["vendor"] = vendor_filter
    if zone_filter:
        conditions.append("vl.delivery_zone = %(zone)s")
        values["zone"] = zone_filter

    where = "WHERE " + " AND ".join(conditions)

    data = frappe.db.sql(f"""
        SELECT
            vs.product,
            p.product_name,
            p.category,
            vs.vendor,
            v.vendor_name,
            vl.delivery_zone,
            vs.available_qty,
            vs.reserved_qty,
            vs.physical_qty,
            vl.price,
            vl.track_inventory,
            vl.allow_backorder,
            CASE
                WHEN vs.available_qty <= 0 AND vl.track_inventory = 1 THEN 'Out of Stock'
                WHEN vs.available_qty < 10 AND vl.track_inventory = 1 THEN 'Low Stock'
                WHEN vl.track_inventory = 0 THEN 'Not Tracked'
                ELSE 'In Stock'
            END as stock_status
        FROM `tabVendor Stock` vs
        LEFT JOIN `tabProduct` p ON vs.product = p.name
        LEFT JOIN `tabVendor` v ON vs.vendor = v.name
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
        {"label": "Product", "fieldname": "product", "fieldtype": "Link", "options": "Product", "width": 120},
        {"label": "Name", "fieldname": "product_name", "fieldtype": "Data", "width": 180},
        {"label": "Category", "fieldname": "category", "fieldtype": "Link", "options": "Category", "width": 120},
        {"label": "Vendor", "fieldname": "vendor", "fieldtype": "Link", "options": "Vendor", "width": 120},
        {"label": "Vendor Name", "fieldname": "vendor_name", "fieldtype": "Data", "width": 160},
        {"label": "Zone", "fieldname": "delivery_zone", "fieldtype": "Link", "options": "Delivery Zone", "width": 130},
        {"label": "Available", "fieldname": "available_qty", "fieldtype": "Float", "width": 80},
        {"label": "Reserved", "fieldname": "reserved_qty", "fieldtype": "Float", "width": 80},
        {"label": "Price", "fieldname": "price", "fieldtype": "Currency", "width": 80},
        {"label": "Status", "fieldname": "stock_status", "fieldtype": "Data", "width": 100},
    ]

    return columns, data
