import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import today


class SMCoupon(Document):
    def validate(self):
        if self.coupon_type == "Percentage" and (self.discount_percentage or 0) <= 0:
            frappe.throw(_("Discount percentage must be greater than 0"))
        if self.coupon_type == "Fixed Amount" and (self.discount_amount or 0) <= 0:
            frappe.throw(_("Discount amount must be greater than 0"))


def validate_coupon(coupon_code, order_subtotal, user=None, franchise=None):
    doc = frappe.db.get_value(
        "SM Coupon",
        {"coupon_code": coupon_code, "is_active": 1},
        ["name", "coupon_type", "discount_percentage", "discount_amount",
         "min_order_amount", "max_discount_amount", "max_uses", "used_count",
         "valid_from", "valid_to", "applicable_vendors", "max_uses_per_user"],
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

    if doc.applicable_vendors:
        allowed = [v.strip() for v in doc.applicable_vendors.split(",") if v.strip()]
        if franchise and allowed and franchise not in allowed:
            frappe.throw(_("This coupon is not valid for the selected store"))

    if doc.max_uses_per_user and doc.max_uses_per_user > 0 and user:
        user_uses = frappe.db.count("SM Coupon Usage", {"coupon": doc.name, "user": user})
        if user_uses >= doc.max_uses_per_user:
            frappe.throw(_("You have already used this coupon the maximum number of times"))

    if (doc.min_order_amount or 0) > 0 and order_subtotal < doc.min_order_amount:
        frappe.throw(_("Minimum order amount for this coupon is NPR {0}").format(doc.min_order_amount))

    if doc.coupon_type == "Percentage":
        discount = order_subtotal * (doc.discount_percentage / 100)
        if doc.max_discount_amount:
            discount = min(discount, doc.max_discount_amount)
        return {"discount": round(discount, 2), "free_delivery": False, "coupon_type": doc.coupon_type}
    elif doc.coupon_type == "Fixed Amount":
        return {"discount": round(min(doc.discount_amount, order_subtotal), 2), "free_delivery": False, "coupon_type": doc.coupon_type}
    else:
        return {"discount": 0, "free_delivery": True, "coupon_type": doc.coupon_type}


def increment_coupon_usage(coupon_code, user=None, order=None):
    doc = frappe.db.get_value("SM Coupon", {"coupon_code": coupon_code}, "name")
    if not doc:
        return

    frappe.db.sql(
        "UPDATE `tabSM Coupon` SET used_count = used_count + 1 WHERE name = %(name)s AND (max_uses = 0 OR used_count < max_uses)",
        {"name": doc},
    )

    if user:
        frappe.get_doc({
            "doctype": "SM Coupon Usage",
            "coupon": doc,
            "user": user,
            "order": order,
        }).insert(ignore_permissions=True)
