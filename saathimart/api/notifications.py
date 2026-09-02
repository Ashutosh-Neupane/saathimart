"""
Notification system — handles order status updates, email confirmations,
and push notification support for the storefront.
"""
import frappe
from frappe import _
from saathimart.api.responses import handle_api_errors


def create_order_status_notification(doc, new_status):
    """Create a notification when order status changes."""
    status_messages = {
        "Confirmed": "Your order {0} has been confirmed!",
        "Preparing": "Your order {0} is being prepared.",
        "Out for Delivery": "Your order {0} is out for delivery!",
        "Delivered": "Your order {0} has been delivered. Thank you!",
        "Cancelled": "Your order {0} has been cancelled.",
    }

    message = status_messages.get(new_status, "Your order {0} status updated to {1}.")
    message = message.format(doc.name, new_status)

    # Create notification doc
    try:
        notification = frappe.new_doc("Notification Log")
        notification.subject = message
        notification.type = "Information"
        notification.document_type = "Order"
        notification.document_name = doc.name
        notification.from_user = "Administrator"
        notification.insert(ignore_permissions=True)
    except Exception:
        pass  # Notification Log might not exist in all setups

    # Send email if customer email is available
    if doc.customer_email:
        try:
            frappe.sendmail(
                recipients=[doc.customer_email],
                subject="Order {0} — {1}".format(doc.name, new_status),
                message="<p>{0}</p>".format(message),
            )
        except Exception:
            pass


def send_payment_confirmation(email, order_id, amount, items):
    """Send payment confirmation email."""
    if not email:
        return

    items_html = ""
    for item in items:
        items_html += "<li>{0} × {1} — Rs {2}</li>".format(
            item.get("product_name", ""), item.get("qty", 0), item.get("rate", 0)
        )

    html = """
    <h2>Payment Confirmed!</h2>
    <p>Thank you for your order <strong>{order_id}</strong>.</p>
    <p>Amount: <strong>Rs {amount}</strong></p>
    <h3>Items:</h3>
    <ul>{items}</ul>
    <p>We'll notify you when your order is dispatched.</p>
    """.format(order_id=order_id, amount=amount, items=items_html)

    try:
        frappe.sendmail(
            recipients=[email],
            subject="Payment Confirmed — Order {0}".format(order_id),
            message=html,
        )
    except Exception:
        pass


def send_dispatch_notification(email, order_id, vendor_name=""):
    """Send dispatch notification."""
    if not email:
        return
    try:
        frappe.sendmail(
            recipients=[email],
            subject="Order {0} Dispatched".format(order_id),
            message="<p>Your order {0} has been dispatched{1}.</p>".format(
                order_id, " by " + vendor_name if vendor_name else ""
            ),
        )
    except Exception:
        pass


# ── Notification Preferences ───────────────────────────────────────────────────

def get_notification_preferences():
    """Return the current user's notification preference toggles.

    Guests get safe defaults. Logged-in users read from custom User fields
    (notify_order_updates, notify_promotions, notify_delivery_reminders).
    If the fields don't exist yet (migration hasn't run), return defaults.
    """

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
@handle_api_errors
def update_notification_preferences(order_updates=None, promotions=None, delivery_reminders=None):
    """Save the current user's notification preference toggles."""

    if frappe.session.user == "Guest":
        frappe.throw("Login required", frappe.PermissionError)

    user = frappe.get_doc("User", frappe.session.user)
    if order_updates is not None:
        user.notify_order_updates = 1 if order_updates else 0
    if promotions is not None:
        user.notify_promotions = 1 if promotions else 0
    if delivery_reminders is not None:
        user.notify_delivery_reminders = 1 if delivery_reminders else 0
    user.save(ignore_permissions=True)
    frappe.db.commit()
    return get_notification_preferences()


@frappe.whitelist()
@handle_api_errors
def list_notifications(limit=50, page=1):
    """List in-app notifications for the current user (paginated)."""
    from frappe.utils import cint

    user = frappe.session.user
    if user == "Guest":
        return {"notifications": [], "total": 0}

    limit = min(cint(limit) or 50, 100)
    page = max(1, cint(page) or 1)
    offset = (page - 1) * limit

    total = frappe.db.count("SM Notification", {"user": user})
    notifications = frappe.db.sql(
        """SELECT name, kind, title, message, `read`, created_at
           FROM `tabSM Notification`
           WHERE user = %s
           ORDER BY created_at DESC
           LIMIT %s OFFSET %s""",
        (user, limit, offset),
        as_dict=True,
    )
    return {
        "notifications": notifications,
        "total": total,
        "unread_count": frappe.db.count("SM Notification", {"user": user, "read": 0}),
    }


@frappe.whitelist()
@handle_api_errors
def mark_notifications_read(names=None, mark_all=False):
    """Mark one or all notifications as read."""

    user = frappe.session.user
    if user == "Guest":
        frappe.throw("Login required", frappe.PermissionError)

    if mark_all:
        frappe.db.sql(
            "UPDATE `tabSM Notification` SET `read`=1 WHERE user=%s AND `read`=0",
            user,
        )
        frappe.db.commit()
        return {"ok": True}

    if names:
        if isinstance(names, str):
            names = [n.strip() for n in names.split(",") if n.strip()]
        for name in names:
            doc = frappe.get_doc("SM Notification", name)
            if doc.user == user:
                doc.set("read", 1)
                doc.save(ignore_permissions=True)
        frappe.db.commit()
    return {"ok": True}
