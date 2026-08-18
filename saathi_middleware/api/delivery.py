"""
Delivery API — franchise-based delivery charge estimation.

Endpoints:
  calculate_delivery_charge — GET /api/method/saathi_middleware.api.delivery.calculate_delivery_charge
  list_delivery_zones       — GET /api/method/saathi_middleware.api.delivery.list_delivery_zones
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


@frappe.whitelist(allow_guest=True)
def list_delivery_zones():
    """
    Each active Franchise as a checkout-time "delivery zone" — this
    middleware has no separate zone concept, franchises already are the
    delivery-charge unit (base charge + free-km + per-km rate). Matches
    the frontend's DeliveryZone type (lib/actions/address.ts).

    free_delivery_above has no backing data (Franchise's free-delivery
    field is a distance threshold — free_delivery_upto_km — not an order
    -amount one), so it's always 0 rather than fabricating a number.
    """
    franchises = frappe.get_all(
        "Franchise",
        filters={"status": "Active"},
        fields=[
            "name", "franchise_name", "city", "delivery_base_charge",
            "latitude", "longitude", "serviceable_radius_km",
        ],
    )
    return [
        {
            "name": f.name,
            "zone_name": f.franchise_name,
            "city": f.city,
            "delivery_charge": flt(f.delivery_base_charge),
            "free_delivery_above": 0,
            "label": f"{f.franchise_name} — Rs. {flt(f.delivery_base_charge):g} delivery",
            # Lets the frontend check "is location X still inside the
            # franchise my cart is bound to" client-side, without a round
            # trip per location change (see lib/location/franchise-zones.ts).
            "latitude": flt(f.latitude) if f.latitude else None,
            "longitude": flt(f.longitude) if f.longitude else None,
            "serviceable_radius_km": flt(f.serviceable_radius_km) if f.serviceable_radius_km else None,
        }
        for f in franchises
    ]
