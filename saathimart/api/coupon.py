"""
Coupon API — admin coupon management and validation.

Endpoints:
  - list_coupons()       : List all coupons with usage stats (admin only)
  - get_coupon_usage()   : Get usage stats for a specific coupon (admin only)
  - validate_coupon_api() : Storefront coupon validation (returns reason instead of throwing)
"""
import frappe
from frappe import _
from frappe.utils import flt
from saathimart.api.responses import handle_api_errors


@frappe.whitelist()
@handle_api_errors
def list_coupons():
    """List all coupons with usage stats (admin only)."""
    if "SM Admin" not in frappe.get_roles():
        frappe.throw(_("Not permitted"), frappe.PermissionError)

    coupons = frappe.get_all(
        "Coupon",
        fields=["name", "coupon_code", "coupon_type", "is_active",
                "discount_percentage", "discount_amount",
                "min_order_amount", "max_discount_amount",
                "max_uses", "used_count", "max_uses_per_user",
                "valid_from", "valid_to", "creation"],
        order_by="creation desc",
    )

    result = []
    for c in coupons:
        remaining = None
        if c.max_uses:
            remaining = max(0, (c.max_uses or 0) - (c.used_count or 0))

        result.append({
            "name": c.name,
            "coupon_code": c.coupon_code,
            "coupon_type": c.coupon_type,
            "is_active": c.is_active,
            "discount": c.discount_percentage if c.coupon_type == "Percentage" else c.discount_amount,
            "min_order_amount": flt(c.min_order_amount or 0),
            "max_discount_amount": flt(c.max_discount_amount or 0),
            "max_uses": c.max_uses,
            "used_count": c.used_count or 0,
            "remaining": remaining,
            "max_uses_per_user": c.max_uses_per_user,
            "valid_from": str(c.valid_from) if c.valid_from else None,
            "valid_to": str(c.valid_to) if c.valid_to else None,
        })

    return result


@frappe.whitelist()
@handle_api_errors
def get_coupon_usage(coupon_name):
    """Get detailed usage stats for a specific coupon (admin only)."""
    if "SM Admin" not in frappe.get_roles():
        frappe.throw(_("Not permitted"), frappe.PermissionError)

    coupon = frappe.get_doc("Coupon", coupon_name)
    usages = frappe.get_list(
        "Coupon Usage",
        filters={"coupon": coupon_name},
        fields=["name", "order", "customer_phone", "customer_email",
                "discount_amount", "used_at"],
        order_by="used_at desc",
        limit_page_length=100,
    )

    return {
        "coupon_code": coupon.coupon_code,
        "coupon_type": coupon.coupon_type,
        "is_active": coupon.is_active,
        "total_uses": coupon.used_count or 0,
        "max_uses": coupon.max_uses,
        "usages": usages,
    }


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def validate_coupon_api(coupon_code, order_subtotal=0, customer_phone=None):
    """Storefront coupon validation — returns reason instead of throwing.

    Unlike the internal validate_coupon (which raises ValidationError),
    this returns a user-friendly response so the frontend can show the
    error message inline.
    """
    from saathimart.saathimart.doctype.coupon.coupon import validate_coupon

    if not coupon_code:
        return {"ok": False, "error": _("Coupon code is required")}

    try:
        result = validate_coupon(coupon_code, flt(order_subtotal), customer_phone)
        return {
            "ok": True,
            "discount": result.get("discount", 0),
            "discount_type": result.get("discount_type", ""),
            "free_delivery": result.get("free_delivery", False),
            "message": _("Coupon applied"),
        }
    except frappe.ValidationError as e:
        return {"ok": False, "error": str(e)}
