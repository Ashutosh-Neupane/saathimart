"""
Delivery API — delivery zones, charges, and time estimation.

Endpoints:
  get_delivery_zones  — GET /api/method/saathimart.api.delivery.get_delivery_zones
  estimate_delivery   — GET /api/method/saathimart.api.delivery.estimate_delivery
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt
from saathimart.api.responses import handle_api_errors


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def get_delivery_zones():
    """
    Return all active delivery zones with charges and free-delivery thresholds.
    Frontend uses this to populate the delivery zone selector at checkout.
    """
    zones = frappe.get_list(
        "Delivery Zone",
        filters={"is_active": 1},
        fields=["name", "zone_name", "city", "districts", "delivery_charge",
                "free_delivery_above", "estimated_days"],
        order_by="zone_name asc",
    )
    return [
        {
            "name": z.name,
            "zone_name": z.zone_name,
            "city": z.city,
            "districts": z.districts,
            "delivery_charge": flt(z.delivery_charge or 0),
            "free_delivery_above": flt(z.free_delivery_above or 0),
            "estimated_days": int(z.estimated_days or 1),
            "label": f"{z.zone_name} ({z.city or ''}) — NPR {flt(z.delivery_charge or 0):.0f}",
        }
        for z in zones
    ]


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def estimate_delivery(zone_name, order_total=None):
    """
    Return delivery charge and estimated time for a given zone and order total.
    """
    if not zone_name:
        frappe.throw(_("zone_name is required"))

    zone = frappe.get_doc("Delivery Zone", zone_name)
    if not zone.is_active:
        frappe.throw(_("Delivery zone is not active"))

    charge = flt(zone.delivery_charge or 0)
    free_threshold = flt(zone.free_delivery_above or 0)

    if order_total and free_threshold and flt(order_total) >= free_threshold:
        charge = 0.0

    return {
        "zone": zone.zone_name,
        "city": zone.city,
        "delivery_charge": charge,
        "free_delivery_above": free_threshold,
        "is_free": charge == 0.0,
        "estimated_days": int(zone.estimated_days or 1),
        "estimated_text": f"{zone.estimated_days or 1} day{'s' if (zone.estimated_days or 1) > 1 else ''}",
    }


def free_delivery_threshold(vendor_doc):
    """Order value above which delivery is free for a vendor.

    Checks the vendor's own free_delivery_above setting first;
    falls back to the global Settings value.
    """
    per_vendor = flt(getattr(vendor_doc, "free_delivery_above", 0) or 0)
    if per_vendor > 0:
        return per_vendor
    settings = frappe.get_single("Settings")
    return flt(getattr(settings, "free_delivery_above", 0) or 0)


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def get_delivery_summary(zone_name=None, order_total=None):
    """Compact delivery summary: charge, free threshold, and whether it's free.

    Useful for the cart page to show 'Add Rs X more for free delivery'.
    """
    from saathimart.api.cart import _get_or_create_cart, find_active_cart
    from saathimart.api.utils import guest_rate_limit
    guest_rate_limit("delivery.summary", limit=60, window_seconds=60)

    if not zone_name:
        # Try to get from cart
        cart_name = find_active_cart()
        if cart_name:
            cart = frappe.get_doc("Cart", cart_name)
            zone_name = cart.delivery_zone

    if not zone_name:
        return {"delivery_charge": 0, "free_delivery_above": 0, "is_free": True, "amount_to_free": 0}

    zone = frappe.get_doc("Delivery Zone", zone_name)
    charge = flt(zone.delivery_charge or 0)
    free_above = flt(zone.free_delivery_above or 0)
    total = flt(order_total or 0)

    is_free = (free_above > 0 and total >= free_above) if free_above else (charge == 0)
    amount_to_free = max(0, free_above - total) if free_above and not is_free else 0

    return {
        "delivery_charge": charge if not is_free else 0,
        "free_delivery_above": free_above,
        "is_free": is_free,
        "amount_to_free": amount_to_free,
    }
