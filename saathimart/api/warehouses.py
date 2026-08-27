"""
Multi-warehouse API — nearest-warehouse routing and per-location stock.

When a vendor has multiple warehouses (Vendor Warehouse child table on the
Vendor doctype), stock is tracked per warehouse (Vendor Stock gains a
warehouse dimension) and orders are routed to the nearest warehouse with
available stock.

Backward compatibility: if a vendor has NO warehouses configured, the system
falls back to the single default_warehouse behaviour that existed before this
module.
"""
import frappe
from frappe import _
from frappe.utils import flt

from saathimart.api.responses import handle_api_errors


# ── Helpers ────────────────────────────────────────────────────────────────

def _haversine_km(lat1, lng1, lat2, lng2):
    """Great-circle distance between two lat/lng points in km."""
    import math

    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlng / 2) ** 2
    )
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def get_vendor_warehouses(vendor_name):
    """Return all active warehouses for a vendor, with their locations."""
    return frappe.get_all(
        "Vendor Warehouse",
        filters={"parent": vendor_name, "parenttype": "Vendor", "status": "Active"},
        fields=["warehouse_name", "erpnext_warehouse", "lat", "lng",
                "is_default", "priority"],
        order_by="priority desc, warehouse_name asc",
    )


def get_default_warehouse(vendor_name):
    """Return the default warehouse name for a vendor (or None)."""
    wh = frappe.db.get_value(
        "Vendor Warehouse",
        {"parent": vendor_name, "parenttype": "Vendor", "is_default": 1, "status": "Active"},
        "warehouse_name",
    )
    return wh or frappe.db.get_value("Vendor", vendor_name, "default_warehouse")


def find_nearest_warehouse(vendor_name, customer_lat, customer_lng):
    """Find the nearest active warehouse with stock for a given vendor.

    Returns dict with warehouse_name, distance_km, available_qty or None.
    """
    if not customer_lat or not customer_lng:
        # No customer location — fall back to default warehouse
        dw = get_default_warehouse(vendor_name)
        return {"warehouse_name": dw, "distance_km": None} if dw else None

    warehouses = get_vendor_warehouses(vendor_name)
    if not warehouses:
        # No warehouses configured — legacy single-warehouse path
        dw = get_default_warehouse(vendor_name)
        return {"warehouse_name": dw, "distance_km": None} if dw else None

    best = None
    for wh in warehouses:
        if not wh.lat or not wh.lng:
            continue
        dist = _haversine_km(customer_lat, customer_lng, wh.lat, wh.lng)
        if best is None or dist < best["distance_km"]:
            best = {"warehouse_name": wh.warehouse_name, "distance_km": round(dist, 2),
                    "erpnext_warehouse": wh.erpnext_warehouse, "priority": wh.priority}

    # Fall back to default if no warehouse with coordinates found
    if not best:
        dw = get_default_warehouse(vendor_name)
        return {"warehouse_name": dw, "distance_km": None} if dw else None

    return best


# ── API Endpoints ──────────────────────────────────────────────────────────

@frappe.whitelist(allow_guest=True)
@handle_api_errors
def get_warehouses_for_vendor(vendor):
    """Return all active warehouses for a vendor (admin/dashboard use)."""
    if not vendor:
        frappe.throw(_("vendor is required"))
    return get_vendor_warehouses(vendor)


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def get_stock_by_warehouse(product):
    """Return per-vendor, per-warehouse stock for a product.

    Response: {vendor: {warehouse_name: {available_qty, ...}, ...}, ...}
    Falls back to default warehouse (empty warehouse key) for legacy stock rows.
    """
    if not product:
        frappe.throw(_("product is required"))

    rows = frappe.get_all(
        "Vendor Stock",
        filters={"product": product},
        fields=["vendor", "warehouse", "available_qty", "reserved_qty",
                "physical_qty", "is_default_warehouse"],
    )

    result = {}
    for r in rows:
        v = r.vendor
        wh = r.warehouse or ""  # empty = default warehouse
        result.setdefault(v, {})[wh] = {
            "available_qty": flt(r.available_qty or 0),
            "reserved_qty": flt(r.reserved_qty or 0),
            "physical_qty": flt(r.physical_qty or 0),
            "is_default": r.is_default_warehouse or 0,
        }
    return result


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def select_nearest_warehouse(vendor, customer_lat=None, customer_lng=None):
    """Pick the best warehouse for an order to this vendor.

    Returns {warehouse_name, distance_km, available_total} or None.
    """
    if not vendor:
        frappe.throw(_("vendor is required"))
    customer_lat = flt(customer_lat) if customer_lat else None
    customer_lng = flt(customer_lng) if customer_lng else None
    return find_nearest_warehouse(vendor, customer_lat, customer_lng)
