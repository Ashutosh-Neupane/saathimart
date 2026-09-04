import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, now_datetime, today


class Coupon(Document):
    def validate(self):
        if self.coupon_type == "Percentage" and (self.discount_percentage or 0) <= 0:
            frappe.throw(_("Discount percentage must be greater than 0"))
        if self.coupon_type == "Fixed Amount" and (self.discount_amount or 0) <= 0:
            frappe.throw(_("Discount amount must be greater than 0"))


def validate_coupon(coupon_code, order_subtotal, customer_phone=None):
    """Returns dict with discount amount and free_delivery flag, or raises ValidationError.

    `customer_phone` is the per-customer limit key rather than email or user:
    checkout is open to guests, who supply a phone and no account, so keying
    the limit on the logged-in user would let anyone bypass max_uses_per_user
    by checking out as a guest.
    """
    doc = frappe.db.get_value(
        "Coupon",
        {"coupon_code": coupon_code, "is_active": 1},
        ["name", "coupon_type", "discount_percentage", "discount_amount",
         "min_order_amount", "max_discount_amount", "max_uses", "used_count",
         "max_uses_per_user", "valid_from", "valid_to"],
        as_dict=True,
    )
    if not doc:
        frappe.throw(_("Invalid or inactive coupon code"))

    if doc.valid_from and str(doc.valid_from) > today():
        frappe.throw(_("Coupon is not yet valid"))
    if doc.valid_to and str(doc.valid_to) < today():
        frappe.throw(_("Coupon has expired"))
    if doc.max_uses and (doc.used_count or 0) >= doc.max_uses:
        frappe.throw(_("Coupon usage limit reached"))
    if (doc.min_order_amount or 0) > 0 and order_subtotal < doc.min_order_amount:
        frappe.throw(_("Minimum order amount for this coupon is NPR {0}").format(doc.min_order_amount))

    # Per-customer limit. Previously the signature took a `user` argument that
    # was never read, so max_uses_per_user — which defaults to 1 on every
    # coupon — was silently unenforced and a single-use code could be redeemed
    # forever by the same person.
    if doc.max_uses_per_user and customer_phone:
        used_by_customer = frappe.db.count(
            "Coupon Usage", {"coupon": doc.name, "customer_phone": customer_phone}
        )
        if used_by_customer >= doc.max_uses_per_user:
            frappe.throw(_("You have already used this coupon"))

    if doc.coupon_type == "Percentage":
        discount = order_subtotal * (doc.discount_percentage / 100)
        if doc.max_discount_amount:
            discount = min(discount, doc.max_discount_amount)
        return {"discount": round(discount, 2), "discount_type": "percentage", "free_delivery": False}
    elif doc.coupon_type == "Fixed Amount":
        return {"discount": round(min(doc.discount_amount, order_subtotal), 2), "discount_type": "fixed", "free_delivery": False}
    else:  # Free Delivery
        return {"discount": 0, "discount_type": "free_delivery", "free_delivery": True}


def increment_coupon_usage(coupon_code, order=None, customer_phone=None,
                           customer_email=None, discount_amount=0):
    """Bump the global counter and, when an order is given, write the usage row
    that per-customer limits are counted from.

    The row is keyed on `order` (it is the docname), so a retried checkout for
    the same order cannot double-count. Without a row, max_uses_per_user has
    nothing to count and stays unenforced.
    """
    frappe.db.sql(
        "UPDATE `tabCoupon` SET used_count = used_count + 1 WHERE coupon_code = %(coupon_code)s",
        {"coupon_code": coupon_code},
    )

    if not order:
        return
    if frappe.db.exists("Coupon Usage", order):
        return

    coupon_name = frappe.db.get_value("Coupon", {"coupon_code": coupon_code}, "name")
    if not coupon_name:
        return

    frappe.get_doc({
        "doctype": "Coupon Usage",
        "coupon": coupon_name,
        "order": order,
        "customer_phone": customer_phone or "",
        "customer_email": customer_email or "",
        "discount_amount": discount_amount or 0,
        "used_at": now_datetime(),
    }).insert(ignore_permissions=True)


def decrement_coupon_usage(coupon_code, order=None):
    """Reverse a coupon usage — called when an order is refunded/cancelled.

    Decrements the global counter and removes the Coupon Usage row so the
    customer's per-user limit is freed up.
    """
    frappe.db.sql(
        "UPDATE `tabCoupon` SET used_count = GREATEST(used_count - 1, 0) WHERE coupon_code = %(coupon_code)s",
        {"coupon_code": coupon_code},
    )
    if order:
        frappe.db.delete("Coupon Usage", {"order": order})


@frappe.whitelist(allow_guest=True)
def check_coupon(coupon_code, order_subtotal=0, customer_phone=None):
    """Storefront-facing validity check that answers instead of raising.

    validate_coupon throws precise, already-translated messages ("Coupon has
    expired", "Minimum order amount for this coupon is NPR 500"), but the only
    caller was the totals engine, which swallows ValidationError and quietly
    sets the discount to zero. A shopper typing a valid-but-expired code saw
    nothing happen and no reason why. This returns the reason instead.
    """
    try:
        result = validate_coupon(coupon_code, flt(order_subtotal), customer_phone)
        return {
            "valid": True,
            "discount": flt(result.get("discount")),
            "free_delivery": bool(result.get("free_delivery")),
            "message": _("Coupon applied"),
        }
    except frappe.ValidationError:
        # frappe.throw queues the text for the response's error banner; take it
        # for our own payload and clear it so the caller gets a clean 200.
        messages = frappe.get_message_log() or []
        text = messages[-1].get("message") if messages else _("Invalid coupon code")
        frappe.clear_messages()
        frappe.local.response.http_status_code = 200
        return {"valid": False, "discount": 0, "free_delivery": False, "message": text}
