"""Backfill legacy warehouse values.

Before the multi-warehouse change, stock and listings lived on a single
implicit warehouse: Vendor Stock rows were always named
"{vendor}-{product}-default" and Vendor Listing rows carried warehouse = NULL.

After the multi-warehouse change the two doctypes gained a warehouse
dimension, but pre-existing rows were never migrated. The drift is
invisible to read paths that ignore the column, but any query that filters
or groups by warehouse (per-warehouse stock checks, fulfillment routing)
would silently miss legacy rows — a listing with warehouse = NULL has no
stock row match when the caller looks up warehouse = 'default'.

This patch normalises both tables to the canonical legacy value "default"
so per-warehouse lookups always find the pre-existing rows.
"""

import frappe


def execute():
    for table in ("tabVendor Stock", "tabVendor Listing"):
        frappe.db.sql(
            f"""
            UPDATE `{table}`
            SET warehouse = 'default'
            WHERE warehouse IS NULL OR warehouse = ''
            """
        )

    frappe.db.commit()