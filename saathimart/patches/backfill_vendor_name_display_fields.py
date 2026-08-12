"""
Patch: backfill Vendor Fulfillment.vendor_name / Vendor Payout.vendor_name
for rows that already existed before these fetch_from fields were added.

Vendor has no explicit autoname, so Vendor.name is either an opaque hash
(desk/test-created records) or the vendor's own vendor_id (self-registered
via api.location.update_vendor_location) — never something a human reads
at a glance. vendor_name is the actual business name, fetched from Vendor
onto each row so the Order form's Vendor Fulfillments grid and the Vendor
Payout list show something readable instead of that id.

Going forward these fields stay in sync automatically — fetch_from applies
on every insert/update of the child/doc going through Frappe's controller.
This patch only needs to run once, to backfill pre-existing rows that were
written before the field existed.

Run via: bench --site <site> migrate
"""
import frappe


def execute():
    frappe.db.sql("""
        UPDATE `tabVendor Fulfillment` vf
        JOIN `tabVendor` v ON v.name = vf.vendor
        SET vf.vendor_name = v.vendor_name
        WHERE vf.vendor_name IS NULL OR vf.vendor_name = ''
    """)
    frappe.db.sql("""
        UPDATE `tabVendor Payout` vp
        JOIN `tabVendor` v ON v.name = vp.vendor
        SET vp.vendor_name = v.vendor_name
        WHERE vp.vendor_name IS NULL OR vp.vendor_name = ''
    """)
