"""
Notification system — handles order status updates, email confirmations,
and push notification support for the storefront.
"""
import frappe
from frappe import _


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
