"""
Location API — nearest-vendor resolution using MariaDB spatial functions.

All methods are whitelisted (allow_guest=True).

Query params:
  lat          — Customer latitude (required for distance calc)
  lng          — Customer longitude (required for distance calc)
  radius_km    — Search radius in km (default: 5)
"""
import re

import frappe
import math

from frappe import _
from frappe.utils import flt, now_datetime


from saathimart.api.utils import guest_rate_limit, verify_hub_secret
from saathimart.api.responses import handle_api_errors


def _bounding_box(lat, lng, radius_km):
    """Return (lat_min, lat_max, lng_min, lng_max) for a given center and radius."""
    lat_delta = radius_km / 111.0
    lng_delta = radius_km / (111.0 * math.cos(math.radians(lat)))
    return lat - lat_delta, lat + lat_delta, lng - lng_delta, lng + lng_delta


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def resolve_vendors(lat, lng, radius_km=5):
    """
    Return active vendors within radius, sorted by distance.
    Uses MariaDB ST_Distance_Sphere for SQL-level distance calculation.
    """
    guest_rate_limit("location.resolve_vendors", limit=100, window_seconds=60)
    lat = flt(lat)
    lng = flt(lng)
    radius_km = flt(radius_km)

    lat_min, lat_max, lng_min, lng_max = _bounding_box(lat, lng, radius_km)
    radius_m = radius_km * 1000

    vendors = frappe.db.sql("""
        SELECT name, vendor_name, lat, lng, service_radius_km,
               address, hub_status, total_available_qty,
               ST_Distance_Sphere(
                   ST_PointFromText(CONCAT('POINT(', lng, ' ', lat, ')')),
                   ST_PointFromText(CONCAT('POINT(', %s, ' ', %s, ')'))
               ) AS distance_meters
        FROM `tabVendor`
        WHERE status = 'Active'
          AND hub_status != 'Suspended'
          AND lat BETWEEN %s AND %s
          AND lng BETWEEN %s AND %s
          AND lat IS NOT NULL AND lng IS NOT NULL
          AND lat != 0 AND lng != 0
          AND ST_Distance_Sphere(
              ST_PointFromText(CONCAT('POINT(', lng, ' ', lat, ')')),
              ST_PointFromText(CONCAT('POINT(', %s, ' ', %s, ')'))
          ) <= COALESCE(NULLIF(service_radius_km, 0), 5) * 1000
        ORDER BY distance_meters ASC
    """, (lng, lat, lat_min, lat_max, lng_min, lng_max, lng, lat), as_dict=True)

    result = []
    for v in vendors:
        result.append({
            "name": v.name,
            "vendor_name": v.vendor_name,
            "lat": flt(v.lat),
            "lng": flt(v.lng),
            "service_radius_km": flt(v.service_radius_km or 5),
            "address": getattr(v, "address", "") or "",
            "distance_km": round(flt(v.distance_meters or 0) / 1000, 2),
            "hub_status": getattr(v, "hub_status", "Active"),
            "product_count": frappe.db.count(
                "Vendor Stock",
                filters={"vendor": v.name, "available_qty": [">", 0]},
            ),
        })

    return result


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def nearest_vendor_for_product(product, lat, lng, radius_km=5):
    """
    Return vendors that have this product in stock, sorted by distance.
    Uses MariaDB ST_Distance_Sphere for SQL-level distance calculation.
    """
    guest_rate_limit("location.nearest_vendor", limit=100, window_seconds=60)
    lat = flt(lat)
    lng = flt(lng)
    radius_km = flt(radius_km)
    radius_m = radius_km * 1000

    listings = frappe.db.sql("""
        SELECT vl.vendor, vl.price, vl.available_qty, vl.reserved_qty,
               vl.delivery_zone, vl.estimated_delivery_minutes, vl.priority,
               v.vendor_name, v.lat, v.lng,
               COALESCE(NULLIF(v.service_radius_km, 0), 5) AS service_radius_km,
               ST_Distance_Sphere(
                   ST_PointFromText(CONCAT('POINT(', v.lng, ' ', v.lat, ')')),
                   ST_PointFromText(CONCAT('POINT(', %s, ' ', %s, ')'))
               ) AS distance_meters
        FROM `tabVendor Listing` vl
        JOIN `tabVendor` v ON vl.vendor = v.name
        WHERE vl.product = %s
          AND vl.status = 'Active'
          AND v.lat IS NOT NULL AND v.lng IS NOT NULL
          AND v.lat != 0 AND v.lng != 0
          AND ST_Distance_Sphere(
              ST_PointFromText(CONCAT('POINT(', v.lng, ' ', v.lat, ')')),
              ST_PointFromText(CONCAT('POINT(', %s, ' ', %s, ')'))
          ) <= COALESCE(NULLIF(v.service_radius_km, 0), 5) * 1000
        ORDER BY distance_meters ASC
    """, (lng, lat, product, lng, lat), as_dict=True)

    result = []
    for l in listings:
        result.append({
            "vendor": l.vendor,
            "vendor_name": getattr(l, "vendor_name", l.vendor),
            "lat": flt(l.lat),
            "lng": flt(l.lng),
            "service_radius_km": flt(l.service_radius_km or 5),
            "available_qty": flt(l.available_qty or 0),
            "reserved_qty": flt(l.reserved_qty or 0),
            "price": flt(l.price or 0),
            "distance_km": round(flt(l.distance_meters or 0) / 1000, 2),
            "delivery_zone": getattr(l, "delivery_zone", "") or "",
            "estimated_delivery_minutes": flt(l.estimated_delivery_minutes or 20),
        })

    return result


def _humanize_vendor_id(vendor_id):
    """Turn a site hostname like 'vendor1.localhost' into a readable label
    ('Vendor1') to seed vendor_name on first contact. Admin can rename later."""
    label = re.split(r"[.:]", vendor_id)[0]
    label = re.sub(r"[_-]+", " ", label).strip()
    return label.title() if label else vendor_id


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def update_vendor_location(vendor_id, lat, lng, service_radius_km=5, address=""):
    """
    Called by saathimart-vendor's sync_vendor_location() to push location updates.

    Creates or updates the Vendor doc on the hub with the vendor's current
    warehouse location and delivery radius. A vendor site the hub has never
    seen before is self-registered under its own vendor_id (as "Pending" —
    it won't be selectable for orders until an admin approves it), so the
    two sides never need their Vendor names manually kept in sync.
    """
    verify_hub_secret("location.update_vendor_location")

    if not vendor_id:
        frappe.throw(_("vendor_id is required"))

    is_new_vendor = not frappe.db.exists("Vendor", vendor_id)
    if is_new_vendor:
        doc = frappe.new_doc("Vendor")
        # autoname is unset (hash-based) for Vendor, so the desired name has
        # to be forced explicitly — name_set tells set_new_name() to leave
        # doc.name alone instead of overwriting it with a generated hash.
        doc.name = vendor_id
        doc.flags.name_set = True
        doc.vendor_name = _humanize_vendor_id(vendor_id)
        doc.status = "Pending"
        doc.commission_pct = 0
    else:
        doc = frappe.get_doc("Vendor", vendor_id)

    doc.lat = flt(lat)
    doc.lng = flt(lng)
    doc.service_radius_km = flt(service_radius_km) or 5
    doc.address = address or getattr(doc, "address", "") or ""
    doc.hub_status = "Active"
    doc.last_sync_at = now_datetime()

    if is_new_vendor:
        doc.insert(ignore_permissions=True)
    else:
        doc.save(ignore_permissions=True)
    frappe.db.commit()

    return {
        "ok": True,
        "vendor": doc.name,
        "newly_registered": is_new_vendor,
        "status": doc.status,
        "lat": doc.lat,
        "lng": doc.lng,
        "service_radius_km": doc.service_radius_km,
    }
