"""
Patch: add performance indexes for Vendor Stock, Order, Webhook Event, and Sync Outbox.

Run via: bench --site <site> migrate
"""
import frappe


def execute():
    indexes = [
        ("Vendor Stock", "vendor", "product"),
        ("Order", "customer_email", "status", "creation"),
        ("Webhook Event", "status", "target_site", "creation"),
        ("Sync Outbox", "status", "next_retry_at", "creation"),
        ("Cart", "session_id", "status"),
        ("Cart Item", "parent", "product", "vendor"),
        ("Vendor Fulfillment", "parent", "vendor"),
        ("Order Item", "parent", "product"),
        ("Vendor Listing", "product", "vendor", "status"),
        ("Product", "slug", "status"),
    ]

    for doctype, *fields in indexes:
        idx_name = f"idx_{doctype.lower().replace(' ', '_')}_{'_'.join(fields)}"
        cols = ", ".join(f"`{f}`" for f in fields)
        sql = f"CREATE INDEX IF NOT EXISTS `{idx_name}` ON `tab{doctype}` ({cols})"
        try:
            frappe.db.sql(sql)
            frappe.db.commit()
            frappe.log_error(f"Created index {idx_name} on {doctype}({', '.join(fields)})", "Patch")
        except Exception as e:
            frappe.log_error(f"Index {idx_name} failed: {e}", "Patch")
