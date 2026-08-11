"""
Notification API — list and manage user notifications.
Requires login.
"""
import frappe
from frappe import _
from frappe.utils import now_datetime


# Customer-facing copy for order status changes that should surface an
# in-app SM Notification. Statuses not listed here (e.g. intermediate,
# vendor-internal states) are silently skipped.
_ORDER_STATUS_NOTIFICATIONS = {
    "Confirmed":         ("Order Confirmed", "Your order {0} has been confirmed and is being prepared."),
    "Preparing":         ("Order Being Prepared", "Your order {0} is being prepared."),
    "Out for Delivery":  ("Out for Delivery", "Your order {0} is out for delivery."),
    "Delivered":         ("Order Delivered", "Your order {0} has been delivered. Thank you for shopping with us!"),
    "Cancelled":         ("Order Cancelled", "Your order {0} has been cancelled."),
    "Refunded":          ("Order Refunded", "Your order {0} has been refunded."),
}


def create_order_status_notification(order_doc, status):
    """
    Insert an in-app SM Notification for a customer-visible Order status
    change. Called from every code path that changes Order.status (admin
    override, and vendor-reported status via api.events) so the SM
    Notification doctype — previously only readable, never written by the
    order flow — actually reflects what's happening to the order.

    Best-effort: silently no-ops if there's no matching User (customer_email
    is a free-text Data field, not a Link, so it isn't guaranteed to match
    an existing User) or no copy configured for this status.
    """
    email = getattr(order_doc, "customer_email", None)
    if not email or not frappe.db.exists("User", email):
        return

    info = _ORDER_STATUS_NOTIFICATIONS.get(status)
    if not info:
        return
    title, message_template = info

    doc = frappe.new_doc("SM Notification")
    doc.user = email
    doc.kind = "order"
    doc.title = title
    doc.message = message_template.format(order_doc.name)
    doc.created_at = now_datetime()
    doc.insert(ignore_permissions=True)


@frappe.whitelist()
def list_notifications(limit=50):
    """Return current user's notifications, newest first."""
    if frappe.session.user == "Guest":
        return []

    limit = min(100, max(1, int(limit)))

    return frappe.get_list(
        "SM Notification",
        filters={"user": frappe.session.user},
        fields=["name", "kind", "title", "message", "read", "created_at"],
        order_by="created_at desc",
        limit_page_length=limit,
    )


@frappe.whitelist()
def mark_notifications_read(names=None):
    """Mark notifications as read. names=None → mark all."""
    if frappe.session.user == "Guest":
        return []

    filters = {"user": frappe.session.user, "read": 0}
    if names:
        if isinstance(names, str):
            names = [n.strip() for n in names.split(",") if n.strip()]
        if names:
            filters["name"] = ["in", names]

    frappe.db.set_value("SM Notification", filters, "read", 1)
    frappe.db.commit()
    return list_notifications()


@frappe.whitelist()
def get_notification_preferences():
    """Return user's notification preference toggles."""
    if frappe.session.user == "Guest":
        return {
            "order_updates": True,
            "promotions": False,
            "delivery_reminders": True,
        }

    user = frappe.get_doc("User", frappe.session.user)
    return {
        "order_updates": bool(getattr(user, "notify_order_updates", 1)),
        "promotions": bool(getattr(user, "notify_promotions", 0)),
        "delivery_reminders": bool(getattr(user, "notify_delivery_reminders", 1)),
    }


@frappe.whitelist()
def update_notification_preferences(prefs):
    """Save notification preference toggles."""
    if frappe.session.user == "Guest":
        frappe.throw(_("Login required"), frappe.PermissionError)

    if not isinstance(prefs, dict):
        prefs = {}

    user = frappe.get_doc("User", frappe.session.user)
    user.notify_order_updates = 1 if prefs.get("order_updates", True) else 0
    user.notify_promotions = 1 if prefs.get("promotions", False) else 0
    user.notify_delivery_reminders = 1 if prefs.get("delivery_reminders", True) else 0
    user.save(ignore_permissions=True)
    frappe.db.commit()
    return get_notification_preferences()
