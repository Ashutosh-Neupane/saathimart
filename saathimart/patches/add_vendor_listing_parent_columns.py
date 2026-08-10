"""
Patch: add parent/parenttype/parentfield columns to Vendor Listing
after marking it as istable=1.
"""
import frappe


def execute():
    columns = ["parent", "parenttype", "parentfield"]
    existing = {row.Field for row in frappe.db.sql("DESCRIBE `tabVendor Listing`", as_dict=True)}
    for col in columns:
        if col not in existing:
            # ALTER TABLE autocommits in MariaDB — frappe.db.sql() refuses
            # DDL for exactly that reason (ImplicitCommitError); sql_ddl()
            # is the framework's own escape hatch for it.
            frappe.db.sql_ddl(f"ALTER TABLE `tabVendor Listing` ADD COLUMN `{col}` VARCHAR(255)")
            frappe.log_error(f"Added column {col} to tabVendor Listing", "Patch")
    frappe.db.commit()
