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


DEFAULT_TOLERANCE_PCT = 5.0  # fallback when a Vendor has no threshold set


def _tolerance_pct(vendor_name):
    """Per-vendor drift tolerance — Vendor.reconciliation_threshold_pct,
    falling back to DEFAULT_TOLERANCE_PCT when unset (0, None, or the
    vendor predates this field). Previously hardcoded as one global
    constant with no way for a hub admin to tune or disable this per
    vendor; the field existed on the *vendor's own* Vendor Config for
    their own separate reconciliation job (saathimart_vendor/tasks.py) but
    had no equivalent here for this job, which the hub actually runs and
    actually controls."""
    pct = flt(frappe.db.get_value("Vendor", vendor_name, "reconciliation_threshold_pct"))
    return pct if pct > 0 else DEFAULT_TOLERANCE_PCT


def reconcile_stock_hourly():
    """Cron: hourly. Checks each vendor's stock against hub records."""
    vendors = frappe.get_all(
        "Vendor",
        filters={"status": "Active", "hub_status": "Active", "reconciliation_enabled": 1},
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

        outcome = correct_or_flag(vendor_name, row.name, product, warehouse, hub_qty, vendor_qty,
                                   reserved_qty=row.reserved_qty)
        if outcome == "corrected":
            corrected += 1
        elif outcome == "flagged":
            issues.append({
                "product": product,
                "warehouse": warehouse,
                "hub_qty": hub_qty,
                "vendor_qty": vendor_qty,
                "mismatch": abs(hub_qty - vendor_qty),
            })

    if issues:
        _create_reconciliation_issue(vendor_name, issues)

    if corrected > 0:
        frappe.db.commit()

    frappe.logger("reconciliation").info(
        f"Vendor {vendor_name}: {corrected} auto-corrected, {len(issues)} flagged"
    )


def correct_or_flag(vendor_name, vendor_stock_name, product, warehouse, hub_qty, vendor_qty, reserved_qty=0):
    """
    Shared correction decision used by the hourly per-product
    reconciliation and stock_snapshot's full-catalog discrepancy report:
    the vendor's ERPNext Bin is the source of truth, so Vendor Stock is
    ALWAYS corrected toward the vendor's real qty. The per-vendor
    threshold (Vendor.reconciliation_threshold_pct) no longer decides
    *whether* we fix the hub's number — only whether the fix is
    "quiet" (within tolerance, routine drift) or logged to an Issue so
    a human can investigate WHY the vendor's own stock.* events missed
    that much (theft, unreported manual adjustment, missed event...).
    Flagging used to mean leaving the stale qty live on the storefront
    indefinitely — worse for customers than the drift itself.

    Returns "corrected" (within tolerance), "flagged" (corrected AND
    beyond tolerance — caller raises an Issue), or "unchanged".
    """
    mismatch = abs(flt(hub_qty) - flt(vendor_qty))
    if mismatch == 0:
        return "unchanged"

    frappe.db.set_value("Vendor Stock", vendor_stock_name, {
        "physical_qty": vendor_qty,
        "available_qty": flt(vendor_qty) - flt(reserved_qty or 0),
        "last_updated": now_datetime(),
    })

    tolerance = max(flt(hub_qty) * _tolerance_pct(vendor_name) / 100, 1)
    if mismatch <= tolerance:
        return "corrected"

    return "flagged"


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
        # "default" is a placeholder, not a real warehouse — omit it so the
        # vendor falls back to its own Vendor Config default_warehouse.
        # Sending it made the vendor query a non-existent Bin("default")
        # and answer qty 0, flagging every unsynced row as a mismatch.
        params = {"product": product}
        if warehouse and warehouse != "default":
            params["warehouse"] = warehouse
        resp = requests.get(
            f"{target_url}/api/method/saathimart_vendor.api.stock.get_stock_qty",
            params=params,
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
            f"  Product: {issue['product']}, Warehouse: {issue['warehouse']}: "
            f"hub had {issue['hub_qty']}, vendor truth {issue['vendor_qty']} "
            f"(diff {issue['mismatch']}) — corrected to vendor qty; investigate "
            f"why the vendor's stock events missed this."
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
