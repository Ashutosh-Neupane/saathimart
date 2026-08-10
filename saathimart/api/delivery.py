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


@frappe.whitelist(allow_guest=True)
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
