"""
Notification API — list and manage user notifications.
Requires login.
"""
import frappe
from frappe import _


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
