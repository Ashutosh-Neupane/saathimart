"""
Data archival — delete old records per Settings retention policy.

Uses Frappe's built-in scheduled task system (no external tools).
"""
import frappe
from frappe.utils import add_days, now_datetime

from saathimart.api.settings import (
    get_retain_webhook_events_days,
    get_retain_sync_outbox_days,
    get_retain_cart_days,
    get_retain_orders_days,
)


def archive_old_data():
    """
    Daily cron — purge old records according to Settings retention fields.
    Safe for large tables because it uses LIMIT + date filter.
    """
    settings = frappe.get_single("Settings")

    # Webhook Events
    webhook_days = get_retain_webhook_events_days()
    cutoff = add_days(now_datetime(), days=-webhook_days)
    frappe.db.sql("""
        DELETE FROM `tabWebhook Event`
        WHERE status IN ('Sent', 'Failed', 'Skipped', 'Dead')
          AND creation < %s
        LIMIT 1000
    """, cutoff)

    # Sync Outbox
    outbox_days = get_retain_sync_outbox_days()
    cutoff = add_days(now_datetime(), days=-outbox_days)
    frappe.db.sql("""
        DELETE FROM `tabSync Outbox`
        WHERE status IN ('Sent', 'Dead')
          AND creation < %s
        LIMIT 1000
    """, cutoff)

    # Old carts
    cart_days = get_retain_cart_days()
    cutoff = add_days(now_datetime(), days=-cart_days)
    old_carts = frappe.db.sql("""
        SELECT name FROM `tabCart`
        WHERE status IN ('Abandoned', 'Merged', 'CheckedOut')
          AND creation < %s
        LIMIT 500
    """, cutoff, as_dict=True)

    for c in old_carts:
        try:
            frappe.delete_doc("Cart", c.name, ignore_permissions=True, force=True)
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"Archive cart {c.name}")

    # Old orders (delivered/cancelled)
    order_days = get_retain_orders_days()
    cutoff = add_days(now_datetime(), days=-order_days)
    old_orders = frappe.db.sql("""
        SELECT name FROM `tabOrder`
        WHERE status IN ('Delivered', 'Cancelled')
          AND creation < %s
        LIMIT 500
    """, cutoff, as_dict=True)

    for o in old_orders:
        try:
            frappe.delete_doc("Order", o.name, ignore_permissions=True, force=True)
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"Archive order {o.name}")

    frappe.db.commit()
