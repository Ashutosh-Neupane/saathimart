"""
Stock snapshot sync — periodic full-stock comparison between hub and vendor.

Problem: Individual stock.update events can be lost or processed out of order,
causing silent drift between hub's Vendor Stock and vendor's Bin quantities.

Solution: Hourly full-stock snapshot where the hub sends ALL current stock
quantities for each product, and the vendor reconciles. This catches any
drift that individual events missed.

Unlike reconciliation.py (which checks specific products), this sends
the complete catalog snapshot so nothing is missed.
"""
import json

import frappe
from frappe import _
from frappe.utils import flt


def generate_stock_snapshot(vendor_name):
    """Generate a full stock snapshot for a vendor.

    Returns list of {product, stock_qty, warehouse} for all products
    this vendor carries. stock_qty here is the hub's own physical_qty —
    the vendor's real ERPNext Bin stays the source of truth (see
    reconciliation.py); this snapshot is compared against, never used to
    overwrite it — see apply_stock_snapshot in saathimart_vendor.
    """
    stock_rows = frappe.get_all(
        "Vendor Stock",
        filters={"vendor": vendor_name, "physical_qty": [">", 0]},
        fields=["product", "physical_qty", "warehouse"],
    )

    snapshot = []
    for row in stock_rows:
        snapshot.append({
            "product": row.product,
            "stock_qty": row.physical_qty,
            "warehouse": getattr(row, "warehouse", "default") or "default",
        })

    return snapshot


def send_stock_snapshot(vendor_name):
    """Send full stock snapshot to a vendor via event.

    Comparison only, never an override — the vendor's own ERPNext Bin stays
    authoritative for its real inventory (matching reconciliation.py's
    direction: hub corrects itself toward the vendor, not the reverse). The
    vendor reports back any discrepancies (see receive.py._handle_stock_snapshot
    on the vendor side) so an admin can investigate what individual
    stock.* events might have missed.
    """
    snapshot = generate_stock_snapshot(vendor_name)

    vendor_doc = frappe.get_doc("Vendor", vendor_name)
    if not vendor_doc.frappe_site_url:
        return

    payload = {
        "event_type": "stock.snapshot",
        "vendor": vendor_name,
        "stock": snapshot,
        "product_count": len(snapshot),
        "timestamp": str(frappe.utils.now_datetime()),
    }

    # Create event
    event = frappe.new_doc("Webhook Event")
    event.event_type = "stock.snapshot"
    event.target_vendor = vendor_name
    event.target_site = vendor_doc.frappe_site_url
    event.payload = json.dumps(payload, default=str)
    event.status = "Queued"
    event.priority = 3  # NORMAL
    event.insert(ignore_permissions=True)
    frappe.db.commit()

    return event.name


def record_stock_snapshot_report(vendor_name, discrepancies):
    """
    Hub-side receiver for the discrepancy report a vendor sends back after
    comparing a stock.snapshot against its own real ERPNext Bin quantities
    (see saathimart_vendor/api/receive.py::_handle_stock_snapshot — the
    actual comparison happens there, against the vendor's own stock, since
    that is the authoritative side; the hub never overwrites it).

    Applies the same tolerance-based correct-or-flag decision
    reconciliation.py's hourly per-product pass uses (see
    reconciliation.correct_or_flag) — a drift the hourly job hadn't
    individually reconciled yet gets fixed here immediately instead of
    only being logged for a human to notice on the next run.
    """
    if not discrepancies:
        return {"ok": True, "discrepancies": 0}

    from saathimart.api.reconciliation import correct_or_flag

    corrected = 0
    flagged = []
    for d in discrepancies:
        product = d.get("product")
        hub_qty = flt(d.get("hub_qty") or 0)
        vendor_qty = flt(d.get("local_qty") or 0)

        row = frappe.db.get_value(
            "Vendor Stock", {"vendor": vendor_name, "product": product},
            ["name", "warehouse", "reserved_qty"], as_dict=True,
        )
        if not row:
            flagged.append(d)
            continue

        outcome = correct_or_flag(
            vendor_name, row.name, product, row.warehouse or "default",
            hub_qty, vendor_qty, reserved_qty=row.reserved_qty,
        )
        if outcome == "corrected":
            corrected += 1
        elif outcome == "flagged":
            flagged.append(d)

    if corrected:
        frappe.db.commit()
    if flagged:
        frappe.log_error(
            title=f"Stock Snapshot Discrepancies — {vendor_name}",
            message=json.dumps(flagged, indent=2, default=str),
        )

    return {"ok": True, "discrepancies": len(discrepancies), "corrected": corrected, "flagged": len(flagged)}


def get_stock_drift_report():
    """Compare hub stock with each vendor's last known stock.

    Returns drift report showing mismatches across all vendors.
    """
    vendors = frappe.get_all("Vendor", fields=["name", "vendor_name"])
    report = []

    for vendor in vendors:
        stock_rows = frappe.get_all(
            "Vendor Stock",
            filters={"vendor": vendor.name},
            fields=["product", "physical_qty", "warehouse"],
        )

        vendor_drift = []
        for row in stock_rows:
            if row.physical_qty > 0:
                vendor_drift.append({
                    "product": row.product,
                    "hub_qty": row.physical_qty,
                    "warehouse": getattr(row, "warehouse", "default"),
                })

        if vendor_drift:
            report.append({
                "vendor": vendor.name,
                "vendor_name": vendor.vendor_name,
                "products": vendor_drift,
                "total_products": len(vendor_drift),
            })

    return report


def sync_all_stock_snapshots():
    """Hourly cron: send full stock snapshot to each active vendor."""
    vendors = frappe.get_all(
        "Vendor",
        filters={"status": "Active"},
        fields=["name", "vendor_name"],
    )

    sent = 0
    for vendor in vendors:
        try:
            event_name = send_stock_snapshot(vendor.name)
            if event_name:
                sent += 1
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                f"Stock snapshot failed for {vendor.name}",
            )

    if sent:
        frappe.logger("stock_snapshot").info(
            f"Sent stock snapshots to {sent}/{len(vendors)} vendors"
        )
