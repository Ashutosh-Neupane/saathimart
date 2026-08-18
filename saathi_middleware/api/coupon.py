"""
Coupon API — validate coupons, apply to orders, track usage.

Backed by Saathi Coupon / Saathi Coupon Franchise / Saathi Coupon Usage;
see saathi_coupon.py's validate_coupon for why usage limits key off
customer_mobile rather than email.
"""
import frappe
from frappe import _
from frappe.utils import flt


@frappe.whitelist(allow_guest=True)
def validate(coupon_code, order_subtotal=0, franchise=None, customer_mobile=None):
    """
    Validate a coupon code and return discount info.
    Used by frontend to preview coupon before checkout.

    Returns:
    {
        "ok": true,
        "discount": 150,
        "discount_type": "Percentage",
        "message": "Coupon applied"
    }
    """
    if not coupon_code:
        return {"ok": False, "message": _("Coupon code is required")}

    try:
        from saathi_middleware.saathi_middleware.doctype.saathi_coupon.saathi_coupon import validate_coupon
        result = validate_coupon(coupon_code, franchise, customer_mobile, flt(order_subtotal))
        return {
            "ok": True,
            "discount": result.get("discount", 0),
            "discount_type": result.get("discount_type", ""),
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
        "Saathi Coupon",
        coupon_code,
        ["name", "discount_type", "usage_limit_total", "usage_limit_per_customer"],
        as_dict=True,
    )
    if not doc:
        frappe.throw(_("Coupon not found"), frappe.DoesNotExistError)

    usage = frappe.get_all(
        "Saathi Coupon Usage",
        filters={"coupon": doc.name},
        fields=["customer_mobile", "order", "discount_amount", "used_at"],
        order_by="used_at desc",
    )

    return {
        "coupon_code": coupon_code,
        "discount_type": doc.discount_type,
        "total_used": len(usage),
        "usage_limit_total": doc.usage_limit_total or 0,
        "usage_limit_per_customer": doc.usage_limit_per_customer or 0,
        "usage": usage,
    }


@frappe.whitelist()
def list_coupons():
    """List all coupons (admin only)."""
    if "SM Admin" not in frappe.get_roles():
        frappe.throw(_("Not permitted"), frappe.PermissionError)

    return frappe.get_all(
        "Saathi Coupon",
        fields=["name", "code", "is_active", "discount_type", "value",
                "min_order_amount", "max_discount_amount", "usage_limit_total",
                "usage_limit_per_customer", "valid_from", "valid_to"],
        order_by="creation desc",
    )
