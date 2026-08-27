"""
Stock reconciliation — compares hub's Vendor Stock records with the vendor's
actual ERPNext Bin quantities. Runs hourly to catch drift from missed events,
manual adjustments, or race conditions.

Two modes:
  - Auto-correct: mismatch within tolerance → silently fix
  - Flag for review: mismatch beyond tolerance → create Issue for admin
"""
import frappe
from frappe import _
from frappe.utils import flt, now_datetime, add_to_date


TOLERANCE_PCT = 5.0  # auto-correct if within 5%


def reconcile_stock_hourly():
    """Cron: hourly. Checks each vendor's stock against hub records."""
    vendors = frappe.get_all(
        "Vendor",
        filters={"status": "Active", "hub_status": "Active"},
        fields=["name", "vendor_name", "frappe_site_url"],
    )
    for v in vendors:
        try:
            _reconcile_vendor(v.name)
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                f"Stock reconciliation failed for {v.vendor_name}",
            )


def _reconcile_vendor(vendor_name):
    """Reconcile stock for one vendor."""
    from saathimart.api.warehouses import get_vendor_warehouses

    # Get hub's view of stock
    hub_stock = frappe.get_all(
        "Vendor Stock",
        filters={"vendor": vendor_name},
        fields=["product", "warehouse", "available_qty", "reserved_qty",
                "physical_qty", "last_sync_at"],
    )

    if not hub_stock:
        return

    issues = []
    corrected = 0

    for row in hub_stock:
        product = row.product
        hub_qty = flt(row.physical_qty or 0)
        warehouse = row.warehouse or "default"

        # Request actual qty from vendor via their stock API
        vendor_qty = _get_vendor_stock_qty(vendor_name, product, warehouse)
        if vendor_qty is None:
            continue  # vendor unreachable — skip, not an error

        mismatch = abs(hub_qty - vendor_qty)
        if mismatch == 0:
            continue

        tolerance = max(hub_qty * TOLERANCE_PCT / 100, 1)

        if mismatch <= tolerance:
            # Auto-correct: within tolerance
            frappe.db.set_value("Vendor Stock", row.name, {
                "physical_qty": vendor_qty,
                "available_qty": vendor_qty - flt(row.reserved_qty or 0),
                "last_updated": now_datetime(),
            })
            corrected += 1
        else:
            # Flag for review
            issues.append({
                "product": product,
                "warehouse": warehouse,
                "hub_qty": hub_qty,
                "vendor_qty": vendor_qty,
                "mismatch": mismatch,
            })

    if issues:
        _create_reconciliation_issue(vendor_name, issues)

    if corrected > 0:
        frappe.db.commit()

    frappe.logger("reconciliation").info(
        f"Vendor {vendor_name}: {corrected} auto-corrected, {len(issues)} flagged"
    )


def _get_vendor_stock_qty(vendor_name, product, warehouse="default"):
    """Request actual stock qty from vendor. Returns None if unreachable."""
    from saathimart.api.warehouses import get_default_warehouse
    from frappe.utils.password import get_decrypted_password

    vendor_url = frappe.db.get_value("Vendor", vendor_name, "frappe_site_url")
    if not vendor_url:
        return None

    import requests
    import urllib.parse

    parsed = urllib.parse.urlparse(vendor_url)
    host_header = parsed.hostname
    target_url = vendor_url
    if host_header in ("localhost", "vendor1.localhost", "vendor2.localhost"):
        target_url = parsed._replace(netloc="vendors:8000").geturl()

    secret = get_decrypted_password("Vendor", vendor_name, "webhook_secret", raise_exception=False) or ""
    import hashlib, hmac as hmac_mod
    from datetime import datetime, timezone
    ts = str(int(datetime.now(timezone.utc).timestamp()))

    try:
        resp = requests.get(
            f"{target_url}/api/method/saathimart_vendor.api.stock.get_stock_qty",
            params={"product": product, "warehouse": warehouse},
            headers={
                "Host": host_header,
                "X-Vendor-ID": vendor_name,
                "X-SM-Timestamp": ts,
                "X-SM-Signature": hmac_mod.new(secret.encode(), f"{ts}.".encode(), hashlib.sha256).hexdigest(),
            },
            timeout=10,
        )
        if resp.ok:
            data = resp.json().get("message", {})
            return flt(data.get("qty", 0))
    except Exception:
        pass
    return None


def _create_reconciliation_issue(vendor_name, issues):
    """Create an Issue for stock mismatches beyond tolerance."""
    vendor_name_display = frappe.db.get_value("Vendor", vendor_name, "vendor_name") or vendor_name
    lines = [f"Stock reconciliation issues for {vendor_name_display}:"]
    for issue in issues:
        lines.append(
            f"  Product: {issue['product']}, Warehouse: {issue['warehouse']}, "
            f"Hub: {issue['hub_qty']}, Vendor: {issue['vendor_qty']}, "
            f"Mismatch: {issue['mismatch']}"
        )

    # Create Issue doctype if it exists (ERPNext)
    try:
        issue = frappe.new_doc("Issue")
        issue.subject = f"Stock Reconciliation — {vendor_name_display} ({len(issues)} mismatches)"
        issue.description = "\n".join(lines)
        issue.priority = "Medium"
        issue.insert(ignore_permissions=True)
    except Exception:
        # Issue doctype might not exist — log instead
        frappe.log_error(
            title="Stock Reconciliation Issues",
            message="\n".join(lines),
        )
