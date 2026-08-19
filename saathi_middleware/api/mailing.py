"""
Mailing utilities for Saathi Middleware.

Sends OTP emails, order confirmation emails, and password reset emails
using Frappe's built-in sendmail.
"""
import frappe
from frappe import _


def _get_site_name():
    try:
        s = frappe.get_single("Saathi Settings")
        return getattr(s, "site_name", None) or "SaathiMart"
    except Exception:
        return "SaathiMart"


def send_otp_email(email, otp, purpose="signup"):
    title = _("Verify your account") if purpose == "signup" else _("Reset your password")
    subtitle = _("Use the code below to complete your verification.")

    content = f"""
<h2 style="margin-top:0;color:#16a34a;">{title}</h2>
<p style="color:#6b7280;">{subtitle}</p>
<div style="background:#16a34a;border-radius:8px;padding:20px;text-align:center;margin:30px 0;">
<span style="font-size:32px;font-weight:800;letter-spacing:10px;color:#fff;">{otp}</span>
</div>
<p style="font-size:14px;color:#6b7280;">This code will expire in 15 minutes. If you did not request this, you can safely ignore this email.</p>
"""

    _send(email, title, content)


def send_order_confirmation(email, order_id, grand_total, items):
    site_name = _get_site_name()
    items_html = ""
    for item in items:
        items_html += f"<li>{item.get('product_name', item.get('product', ''))} — Qty: {item.get('qty', 0)} — NPR {item.get('rate', 0)}</li>"

    content = f"""
<h2 style="margin-top:0;color:#16a34a;">Order Confirmed!</h2>
<p style="color:#6b7280;">Your order <b>{order_id}</b> has been confirmed and is being prepared.</p>
<p style="color:#6b7280;">Total: <b>NPR {grand_total}</b></p>
<h3 style="color:#16a34a;">Items:</h3>
<ul>{items_html}</ul>
<p style="color:#6b7280;">Thank you for shopping with {site_name}!</p>
"""

    _send(email, f"Order {order_id} — Confirmed", content)


def send_password_reset_email(email, otp):
    send_otp_email(email, otp, purpose="password_reset")


def _send(email, subject, content_html):
    try:
        frappe.sendmail(
            recipients=[email],
            subject=subject,
            content=content_html,
            now=True,
        )
    except Exception as e:
        frappe.log_error(
            frappe.get_traceback(),
            f"Email send failed for {email}: {str(e)}",
        )
