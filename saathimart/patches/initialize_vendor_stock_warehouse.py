"""
Patch: Initialize warehouse dimension on existing Vendor Stock rows.

Existing rows have no warehouse field value — they represent the vendor's
default warehouse stock. This patch sets is_default_warehouse=1 on all
existing rows so the multi-warehouse system recognizes them as the fallback.

No data migration is needed: the new warehouse field is optional, and rows
with empty warehouse continue to work exactly as before. The patch just
marks them explicitly for clarity.
"""
import frappe


def execute():
    # Mark all existing Vendor Stock rows (with no warehouse set) as default
    frappe.db.sql("""
        UPDATE `tabVendor Stock`
        SET is_default_warehouse = 1, warehouse = ''
        WHERE (warehouse IS NULL OR warehouse = '')
    """)

    # Mark all existing Vendor Listing rows with a warehouse field as
    # linking to the default warehouse if the vendor has warehouses
    frappe.db.sql("""
        UPDATE `tabVendor Listing` vl
        INNER JOIN `tabVendor` v ON vl.vendor = v.name
        SET vl.warehouse = v.default_warehouse
        WHERE (vl.warehouse IS NULL OR vl.warehouse = '')
          AND v.default_warehouse IS NOT NULL
          AND v.default_warehouse != ''
    """)

    frappe.db.commit()
