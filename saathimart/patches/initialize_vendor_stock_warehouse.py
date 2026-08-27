"""
Patch: Initialize warehouse dimension on existing Vendor Stock rows.

Existing rows have names like {vendor}-{product} (no warehouse suffix).
The new autoname is {vendor}-{product}-{warehouse}, so we rename
existing rows to {vendor}-{product}-default and set the warehouse field
to 'default' for backward compatibility.
"""
import frappe


def execute():
    # 1. Rename existing Vendor Stock rows to include warehouse suffix
    rows = frappe.db.sql("""
        SELECT name, vendor, product FROM `tabVendor Stock`
        WHERE warehouse IS NULL OR warehouse = '' OR warehouse = 'default'
    """, as_dict=True)

    for row in rows:
        new_name = f"{row.vendor}-{row.product}-default"
        if row.name != new_name:
            try:
                frappe.rename_doc("Vendor Stock", row.name, new_name, force=True)
            except Exception:
                pass  # name collision or other issue — skip

    # 2. Set warehouse='default' and is_default_warehouse=1 on all empty-warehouse rows
    frappe.db.sql("""
        UPDATE `tabVendor Stock`
        SET is_default_warehouse = 1, warehouse = 'default'
        WHERE (warehouse IS NULL OR warehouse = '')
    """)

    # 3. Mark existing Vendor Listing rows with default warehouse
    frappe.db.sql("""
        UPDATE `tabVendor Listing` vl
        INNER JOIN `tabVendor` v ON vl.vendor = v.name
        SET vl.warehouse = v.default_warehouse
        WHERE (vl.warehouse IS NULL OR vl.warehouse = '')
          AND v.default_warehouse IS NOT NULL
          AND v.default_warehouse != ''
    """)

    frappe.db.commit()
