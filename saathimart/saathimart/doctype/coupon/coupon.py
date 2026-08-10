import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import today


class Coupon(Document):
    def validate(self):
        if self.coupon_type == "Percentage" and (self.discount_percentage or 0) <= 0:
            frappe.throw(_("Discount percentage must be greater than 0"))
        if self.coupon_type == "Fixed Amount" and (self.discount_amount or 0) <= 0:
            frappe.throw(_("Discount amount must be greater than 0"))


def validate_coupon(coupon_code, order_subtotal, user=None):
    """Returns dict with discount amount and free_delivery flag, or raises ValidationError."""
    doc = frappe.db.get_value(
        "Coupon",
        {"coupon_code": coupon_code, "is_active": 1},
        ["name", "coupon_type", "discount_percentage", "discount_amount",
         "min_order_amount", "max_discount_amount", "max_uses", "used_count",
         "valid_from", "valid_to"],
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

    if doc.coupon_type == "Percentage":
        discount = order_subtotal * (doc.discount_percentage / 100)
        if doc.max_discount_amount:
            discount = min(discount, doc.max_discount_amount)
        return {"discount": round(discount, 2), "free_delivery": False}
    elif doc.coupon_type == "Fixed Amount":
        return {"discount": round(min(doc.discount_amount, order_subtotal), 2), "free_delivery": False}
    else:  # Free Delivery
        return {"discount": 0, "free_delivery": True}


def increment_coupon_usage(coupon_code):
    frappe.db.sql(
        "UPDATE `tabCoupon` SET used_count = used_count + 1 WHERE coupon_code = %(coupon_code)s",
        {"coupon_code": coupon_code},
    )
