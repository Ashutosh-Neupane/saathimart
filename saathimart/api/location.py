"""
Location API — nearest-vendor resolution with Haversine distance.

All methods are whitelisted (allow_guest=True).

Query params:
  lat          — Customer latitude (required for distance calc)
  lng          — Customer longitude (required for distance calc)
  radius_km    — Search radius in km (default: 5)
"""
import math

import frappe
from frappe import _
from frappe.utils import flt, now_datetime


from saathimart.api.utils import guest_rate_limit


def _haversine(lat1, lng1, lat2, lng2):
    """Return distance in km between two lat/lng points."""
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def _bounding_box(lat, lng, radius_km):
    """Return (lat_min, lat_max, lng_min, lng_max) for a given center and radius."""
    lat_delta = radius_km / 111.0
    lng_delta = radius_km / (111.0 * math.cos(math.radians(lat)))
    return lat - lat_delta, lat + lat_delta, lng - lng_delta, lng + lng_delta


@frappe.whitelist(allow_guest=True)
def resolve_vendors(lat, lng, radius_km=5):
    """
    Return active vendors within radius, sorted by distance.
    """
    guest_rate_limit("location.resolve_vendors", limit=100, window_seconds=60)
    lat = flt(lat)
    lng = flt(lng)
    radius_km = flt(radius_km)

    lat_min, lat_max, lng_min, lng_max = _bounding_box(lat, lng, radius_km)

    vendors = frappe.db.sql("""
        SELECT name, vendor_name, lat, lng, service_radius_km,
               address, hub_status, total_available_qty
        FROM `tabVendor`
        WHERE status = 'Active'
          AND hub_status != 'Suspended'
          AND lat BETWEEN %s AND %s
          AND lng BETWEEN %s AND %s
    """, (lat_min, lat_max, lng_min, lng_max), as_dict=True)

    result = []
    for v in vendors:
        vlat = flt(getattr(v, "lat", 0) or 0)
        vlng = flt(getattr(v, "lng", 0) or 0)
        if not vlat or not vlng:
            continue

        dist = _haversine(lat, lng, vlat, vlng)
        if dist > flt(getattr(v, "service_radius_km", 5) or 5):
            continue

        product_count = frappe.db.count(
            "Vendor Stock",
            filters={"vendor": v.name, "available_qty": [">", 0]},
        )

        result.append({
            "name": v.name,
            "vendor_name": v.vendor_name,
            "lat": vlat,
            "lng": vlng,
            "service_radius_km": flt(getattr(v, "service_radius_km", 5)),
            "address": getattr(v, "address", "") or "",
            "distance_km": round(dist, 2),
            "hub_status": getattr(v, "hub_status", "Active"),
            "product_count": product_count,
        })

    result.sort(key=lambda x: x["distance_km"])
    return result


@frappe.whitelist(allow_guest=True)
def nearest_vendor_for_product(product, lat, lng, radius_km=5):
    """
    Return vendors that have this product in stock, sorted by distance.
    """
    guest_rate_limit("location.nearest_vendor", limit=100, window_seconds=60)
    lat = flt(lat)
    lng = flt(lng)
    radius_km = flt(radius_km)

    # Get all active vendor listings for this product
    listings = frappe.get_list(
        "Vendor Listing",
        filters={"product": product, "status": "Active"},
        fields=["vendor", "price", "available_qty", "reserved_qty",
                "delivery_zone", "estimated_delivery_minutes", "priority"],
        order_by="priority desc, price asc",
    )

    if not listings:
        return []

    # Enrich with vendor location
    vendor_names = [l.vendor for l in listings if l.vendor]
    vendor_map = {}
    if vendor_names:
        for v in frappe.get_list(
            "Vendor",
            filters={"name": ["in", vendor_names]},
            fields=["name", "vendor_name", "lat", "lng", "service_radius_km"],
        ):
            vendor_map[v.name] = v

    result = []
    for l in listings:
        v = vendor_map.get(l.vendor)
        if not v:
            continue

        vlat = flt(getattr(v, "lat", 0) or 0)
        vlng = flt(getattr(v, "lng", 0) or 0)
        if not vlat or not vlng:
            continue

        dist = _haversine(lat, lng, vlat, vlng)
        if dist > flt(getattr(v, "service_radius_km", 5) or 5):
            continue

        result.append({
            "vendor": l.vendor,
            "vendor_name": getattr(v, "vendor_name", l.vendor),
            "lat": vlat,
            "lng": vlng,
            "service_radius_km": flt(getattr(v, "service_radius_km", 5)),
            "available_qty": flt(getattr(l, "available_qty", 0) or 0),
            "reserved_qty": flt(getattr(l, "reserved_qty", 0) or 0),
            "price": flt(getattr(l, "price", 0) or 0),
            "distance_km": round(dist, 2),
            "delivery_zone": getattr(l, "delivery_zone", "") or "",
            "estimated_delivery_minutes": getattr(l, "estimated_delivery_minutes", 20) or 20,
        })

    result.sort(key=lambda x: x["distance_km"])
    return result


@frappe.whitelist(allow_guest=True)
def update_vendor_location(vendor_id, lat, lng, service_radius_km=5, address=""):
    """
    Called by saathimart-vendor's sync_vendor_location() to push location updates.

    Creates or updates the Vendor doc on the hub with the vendor's current
    warehouse location and delivery radius.
    """
    if not vendor_id or not frappe.db.exists("Vendor", vendor_id):
        frappe.throw(_("Vendor {0} not found on hub").format(vendor_id))

    doc = frappe.get_doc("Vendor", vendor_id)
    doc.lat = flt(lat)
    doc.lng = flt(lng)
    doc.service_radius_km = flt(service_radius_km)
    doc.address = address or getattr(doc, "address", "") or ""
    doc.hub_status = "Active"
    doc.last_sync_at = now_datetime()
    doc.save(ignore_permissions=True)
    frappe.db.commit()

    return {
        "ok": True,
        "vendor": vendor_id,
        "lat": doc.lat,
        "lng": doc.lng,
        "service_radius_km": doc.service_radius_km,
    }
