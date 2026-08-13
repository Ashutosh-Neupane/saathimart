"""
Notification API — in-app notifications on order status changes, plus
per-user notification preferences. Requires login for everything except
create_order_status_notification, which is called internally from
order.update_order_status (already SM Admin-gated there).

SM Notification carries no SM Customer doctype-level grant (unlike SM
Address) — every customer-facing read/write here goes through these
whitelisted functions with an explicit frappe.session.user filter, so
there's nothing for the raw REST /api/resource/SM Notification endpoint
to leak even if a customer tried to hit it directly.
"""
import frappe
from frappe.utils import now_datetime


# Customer-facing copy for order status changes that should surface an
# in-app SM Notification. Statuses not listed here are silently skipped.
_ORDER_STATUS_NOTIFICATIONS = {
	"Confirmed":        ("Order Confirmed", "Your order {0} has been confirmed and is being prepared."),
	"Preparing":        ("Order Being Prepared", "Your order {0} is being prepared."),
	"Out for Delivery": ("Out for Delivery", "Your order {0} is out for delivery."),
	"Delivered":        ("Order Delivered", "Your order {0} has been delivered. Thank you for shopping with us!"),
	"Cancelled":        ("Order Cancelled", "Your order {0} has been cancelled."),
	"Refunded":         ("Order Refunded", "Your order {0} has been refunded."),
}


def create_order_status_notification(order_doc, status):
	"""
	Insert an in-app SM Notification for a customer-visible Saathi Order
	status change. Best-effort: silently no-ops if there's no matching
	User (customer_email is free-text Data, not a Link, so it isn't
	guaranteed to match an existing User) or no copy configured for this
	status.
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
		ignore_permissions=True,
	)


@frappe.whitelist()
def mark_notifications_read(names=None):
	"""Mark notifications as read. names=None -> mark all for the current user."""
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
	"""Return the current user's notification preference toggles."""
	if frappe.session.user == "Guest":
		return {
			"order_updates": True,
			"promotions": False,
			"delivery_reminders": True,
		}

	user = frappe.get_doc("User", frappe.session.user)
	return {
		"order_updates": bool(user.notify_order_updates),
		"promotions": bool(user.notify_promotions),
		"delivery_reminders": bool(user.notify_delivery_reminders),
	}


@frappe.whitelist()
def update_notification_preferences(prefs):
	"""Save the current user's notification preference toggles."""
	if frappe.session.user == "Guest":
		frappe.throw("Login required", frappe.PermissionError)

	if not isinstance(prefs, dict):
		prefs = {}

	user = frappe.get_doc("User", frappe.session.user)
	user.notify_order_updates = 1 if prefs.get("order_updates", True) else 0
	user.notify_promotions = 1 if prefs.get("promotions", False) else 0
	user.notify_delivery_reminders = 1 if prefs.get("delivery_reminders", True) else 0
	user.save(ignore_permissions=True)
	frappe.db.commit()
	return get_notification_preferences()
