"""
Coupon API — validate coupons, apply to orders, track usage.
"""
import frappe
from frappe import _
from frappe.utils import flt


@frappe.whitelist(allow_guest=True)
def validate(coupon_code, order_subtotal=0, user=None, franchise=None):
    """
    Validate a coupon code and return discount info.
    Used by frontend to preview coupon before checkout.

    Returns:
    {
        "ok": true,
        "discount": 150,
        "free_delivery": false,
        "coupon_type": "Percentage",
        "message": "Coupon applied"
    }
    """
    if not coupon_code:
        return {"ok": False, "message": _("Coupon code is required")}

    try:
        from saathi_middleware.saathi_middleware.doctype.sm_coupon.sm_coupon import validate_coupon
        result = validate_coupon(coupon_code, flt(order_subtotal), user=user, franchise=franchise)
        return {
            "ok": True,
            "discount": result.get("discount", 0),
            "free_delivery": result.get("free_delivery", False),
            "coupon_type": result.get("coupon_type", ""),
            "message": _("Coupon applied"),
        }
    except frappe.ValidationError as e:
        return {"ok": False, "message": str(e)}


@frappe.whitelist()
def get_usage(coupon_code):
    """Get usage stats for a coupon (admin only)."""
    if "SM Admin" not in frappe.get_roles():
        frappe.throw(_("Not permitted"), frappe.PermissionError)

    doc = frappe.db.get_value(
        "SM Coupon",
        {"coupon_code": coupon_code},
        ["name", "coupon_type", "used_count", "max_uses", "max_uses_per_user"],
        as_dict=True,
    )
    if not doc:
        frappe.throw(_("Coupon not found"), frappe.DoesNotExistError)

    per_user = frappe.get_all(
        "SM Coupon Usage",
        filters={"coupon": doc.name},
        fields=["user", "order", "creation"],
        order_by="creation desc",
    )

    return {
        "coupon_code": coupon_code,
        "coupon_type": doc.coupon_type,
        "total_used": doc.used_count or 0,
        "max_uses": doc.max_uses or 0,
        "max_uses_per_user": doc.max_uses_per_user or 0,
        "per_user_usage": per_user,
    }


@frappe.whitelist()
def list_coupons():
    """List all coupons (admin only)."""
    if "SM Admin" not in frappe.get_roles():
        frappe.throw(_("Not permitted"), frappe.PermissionError)

    return frappe.get_all(
        "SM Coupon",
        fields=["name", "coupon_code", "coupon_type", "is_active",
                "discount_percentage", "discount_amount", "min_order_amount",
                "max_discount_amount", "used_count", "max_uses",
                "max_uses_per_user", "valid_from", "valid_to"],
        order_by="creation desc",
    )
