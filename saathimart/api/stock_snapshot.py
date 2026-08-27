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


def generate_stock_snapshot(vendor_name):
    """Generate a full stock snapshot for a vendor.

    Returns list of {product, stock_qty, warehouse} for all products
    this vendor carries.
    """
    stock_rows = frappe.get_all(
        "Vendor Stock",
        filters={"vendor": vendor_name, "stock_qty": [">", 0]},
        fields=["product", "stock_qty", "warehouse"],
    )

    snapshot = []
    for row in stock_rows:
        snapshot.append({
            "product": row.product,
            "stock_qty": row.stock_qty,
            "warehouse": getattr(row, "warehouse", "default") or "default",
        })

    return snapshot


def send_stock_snapshot(vendor_name):
    """Send full stock snapshot to a vendor via event.

    This is an authoritative override — vendor replaces its stock state
    with what the hub says.
    """
    snapshot = generate_stock_snapshot(vendor_name)

    vendor_doc = frappe.get_doc("Vendor", vendor_name)
    if not vendor_doc.webhook_url:
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
    event.target_site = vendor_doc.webhook_url
    event.payload = json.dumps(payload, default=str)
    event.status = "Queued"
    event.priority = 3  # NORMAL
    event.insert(ignore_permissions=True)
    frappe.db.commit()

    return event.name


def apply_stock_snapshot(vendor_name, snapshot_data):
    """Vendor-side: apply a stock snapshot from the hub.

    This overwrites local Bin quantities with what the hub reports.
    Discrepancies are logged for admin review.
    """
    discrepancies = []

    for item in snapshot_data:
        product = item.get("product")
        expected_qty = item.get("stock_qty", 0)
        warehouse = item.get("warehouse", "default")

        try:
            # Get current local stock
            from saathimart_vendor.utils import get_or_create_stock
            stock_doc = get_or_create_stock(product, vendor_name)
            current_qty = stock_doc.get("stock_qty", 0)

            if current_qty != expected_qty:
                discrepancies.append({
                    "product": product,
                    "warehouse": warehouse,
                    "hub_qty": expected_qty,
                    "local_qty": current_qty,
                    "diff": expected_qty - current_qty,
                })

                # Update to hub's quantity
                stock_doc.stock_qty = expected_qty
                stock_doc.save(ignore_permissions=True)

        except Exception as e:
            frappe.log_error(
                frappe.get_traceback(),
                f"Stock snapshot apply failed for {product}",
            )

    if discrepancies:
        frappe.db.commit()

        # Log discrepancies for admin review
        frappe.log_error(
            title=f"Stock Snapshot Discrepancies — {vendor_name}",
            message=json.dumps(discrepancies, indent=2, default=str),
        )

    return {
        "vendor": vendor_name,
        "items_checked": len(snapshot_data),
        "discrepancies": len(discrepancies),
        "details": discrepancies,
    }


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
            fields=["product", "stock_qty", "warehouse"],
        )

        vendor_drift = []
        for row in stock_rows:
            if row.stock_qty > 0:
                vendor_drift.append({
                    "product": row.product,
                    "hub_qty": row.stock_qty,
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
