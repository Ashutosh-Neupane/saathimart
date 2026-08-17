"""
Delivery API — franchise-based delivery charge estimation.

Endpoints:
  calculate_delivery_charge — GET /api/method/saathi_middleware.api.delivery.calculate_delivery_charge
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt

from math import atan2, cos, radians, sin, sqrt


def _haversine_km(lat1, lon1, lat2, lon2):
    r = 6371
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return r * 2 * atan2(sqrt(a), sqrt(1 - a))


@frappe.whitelist(allow_guest=True)
def calculate_delivery_charge(franchise, delivery_latitude=None, delivery_longitude=None):
    if not franchise:
        frappe.throw("franchise is required")

    franchise_doc = frappe.get_doc("Franchise", franchise)
    if franchise_doc.status != "Active":
        frappe.throw(f"Franchise {franchise} is not active")

    distance_km = None
    lat = flt(delivery_latitude) if delivery_latitude is not None else None
    lng = flt(delivery_longitude) if delivery_longitude is not None else None
    if lat is not None and lng is not None and franchise_doc.latitude and franchise_doc.longitude:
        distance_km = _haversine_km(lat, lng, franchise_doc.latitude, franchise_doc.longitude)
        if distance_km > (franchise_doc.serviceable_radius_km or 0):
            frappe.throw(f"Delivery address is outside {franchise_doc.franchise_name}'s serviceable area")

    base = flt(franchise_doc.delivery_base_charge)
    if distance_km is None:
        return {
            "franchise": franchise_doc.franchise_name,
            "distance_km": None,
            "base_charge": base,
            "free_km": flt(franchise_doc.free_delivery_upto_km),
            "per_km_rate": flt(franchise_doc.delivery_per_km_rate),
            "delivery_charge": base,
        }

    free_km = flt(franchise_doc.free_delivery_upto_km)
    chargeable_km = max(0.0, distance_km - free_km)
    delivery_charge = base + chargeable_km * flt(franchise_doc.delivery_per_km_rate)

    return {
        "franchise": franchise_doc.franchise_name,
        "distance_km": round(distance_km, 2),
        "base_charge": base,
        "free_km": free_km,
        "per_km_rate": flt(franchise_doc.delivery_per_km_rate),
        "delivery_charge": round(delivery_charge, 2),
    }
