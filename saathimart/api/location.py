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
        SELECT vl.vendor, vl.price, vs.available_qty, vs.reserved_qty,
               vl.delivery_zone, vl.estimated_delivery_minutes, vl.priority,
               v.vendor_name, v.lat, v.lng,
               COALESCE(NULLIF(v.service_radius_km, 0), 5) AS service_radius_km,
               ST_Distance_Sphere(
                   ST_PointFromText(CONCAT('POINT(', v.lng, ' ', v.lat, ')')),
                   ST_PointFromText(CONCAT('POINT(', %s, ' ', %s, ')'))
               ) AS distance_meters
        FROM `tabVendor Listing` vl
        JOIN `tabVendor` v ON vl.vendor = v.name
        LEFT JOIN `tabVendor Stock` vs
               ON vs.vendor = vl.vendor AND vs.product = vl.product
              AND (vs.is_default_warehouse = 1 OR vs.warehouse = 'default'
                   OR vs.warehouse IS NULL)
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
    else:
        doc = frappe.get_doc("Vendor", vendor_id)

    doc.lat = flt(lat)
    doc.lng = flt(lng)
    doc.service_radius_km = flt(service_radius_km) or 5
    doc.address = address or getattr(doc, "address", "") or ""
    doc.hub_status = "Active"
    doc.last_sync_at = now_datetime()

    # Frappe's _save_passwords() deletes a Password field's __Auth row when
    # its in-memory value is empty — and Password fields always load as None.
    # A plain doc.save() here therefore silently wiped the per-vendor
    # webhook_secret issued moments earlier by register_vendor (both run in
    # the boot handshake), desyncing every hub→vendor signature. Locations
    # never touch password fields, so skip password handling entirely.
    doc.flags.ignore_save_passwords = True

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


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def register_vendor(vendor_id, site_url, lat=None, lng=None,
                    service_radius_km=5, address=""):
    """
    Full self-registration handshake for a saathimart-vendor site.

    update_vendor_location() only pushes coordinates — the hub was left with
    no `frappe_site_url` to deliver events to and no per-vendor webhook
    secret to sign them with, so hub→vendor delivery never worked for
    self-registered sites. This endpoint closes the loop:

      1. Authenticates the caller. A brand-new vendor_id proves itself with
         the shared bootstrap secret (SaathiMart Settings.webhook_secret);
         an already-registered one must sign with its own per-vendor secret
         (rotation flow unchanged).
      2. Persists `frappe_site_url` — the delivery address the hub's event
         publisher targets (Host header comes from this URL).
      3. Issues (or re-issues) a per-vendor webhook secret and returns it —
         over HTTPS in production — so the vendor stores it in Vendor Config
         and every subsequent push verifies per-vendor, not globally.

    Returns {vendor, secret} on success.
    """
    from saathimart.api.utils import verify_hub_secret
    verify_hub_secret("location.register_vendor", allow_bootstrap=True)

    if not vendor_id:
        frappe.throw(_("vendor_id is required"))
    if not site_url:
        frappe.throw(_("site_url is required"))

    site_url = site_url.strip().rstrip("/")
    vendor_id = vendor_id.strip()

    is_new = not frappe.db.exists("Vendor", vendor_id)
    if is_new:
        doc = frappe.new_doc("Vendor")
        doc.name = vendor_id
        doc.flags.name_set = True
        doc.vendor_name = _humanize_vendor_id(vendor_id)
        doc.status = "Pending"
    else:
        doc = frappe.get_doc("Vendor", vendor_id)

    doc.frappe_site_url = site_url
    if lat is not None:
        doc.lat = flt(lat)
    if lng is not None:
        doc.lng = flt(lng)
    if service_radius_km:
        doc.service_radius_km = flt(service_radius_km) or 5
    if address:
        doc.address = address
    doc.hub_status = "Active"
    doc.last_sync_at = now_datetime()

    # Same password-wipe guard as update_vendor_location above.
    doc.flags.ignore_save_passwords = True

    if is_new:
        doc.insert(ignore_permissions=True)
    else:
        doc.save(ignore_permissions=True)

    # Issue a per-vendor secret. Overwrite only when the vendor has none (or
    # explicitly asked for rotation) — otherwise leave the existing one so
    # re-registration from a restarted container doesn't invalidate a secret
    # the vendor may still be propagating to other workers.
    from frappe.utils.password import get_decrypted_password, set_encrypted_password
    import secrets as _secrets

    current = get_decrypted_password(
        "Vendor", doc.name, "webhook_secret", raise_exception=False
    ) or ""
    if not current:
        current = f"smwh-{_secrets.token_urlsafe(32)}"
        set_encrypted_password("Vendor", doc.name, current, "webhook_secret")
    doc.add_comment("Edit", f"Registered vendor site {site_url}")
    frappe.db.commit()

    return {
        "ok": True,
        "vendor": doc.name,
        "newly_registered": is_new,
        "status": doc.status,
        "secret": current,
    }
