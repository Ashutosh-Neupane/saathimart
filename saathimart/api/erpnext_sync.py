"""
ERPNext Daily Sync for Saathimart

Pushes e-commerce data from saathimart to a separate ERPNext site once per day
for accounting and tax compliance. saathimart remains the single source of truth
for all e-commerce operations; ERPNext is used only for:

  - Customer master records
  - Sales Orders (one per paid saathimart Order)
  - Sales Invoices (submitted, linked to the Sales Order)

What is NOT synced (out of scope for daily batch sync):
  - Vendor stock — vendors manage stock in their own ERPNext via saathimart-vendor
  - Products — admin manages Items directly in ERPNext
  - Real-time payment events — those are handled by accounting.py GL entries

Configuration:
  Settings > ERPNext Integration section must be filled in and
  Settings.erpnext_sync_enabled must be checked.

Run manually:
  bench --site <site> execute saathimart.api.erpnext_sync.run_daily_sync

Scheduled automatically via hooks.py daily scheduler.
"""
from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import flt, nowdate, now_datetime, add_days


# ── ERPNext REST client ───────────────────────────────────────────────────────

class ERPNextSyncError(Exception):
    pass


def _get_config():
    """Read ERPNext connection settings from Settings doctype.

    Returns a plain dict or None when sync is disabled / not configured.
    Never raises — callers treat None as "skip".
    """
    try:
        s = frappe.get_single("Settings")
    except Exception:
        return None

    if not s.erpnext_sync_enabled:
        return None

    site_url  = (s.erpnext_site_url or "").rstrip("/")
    api_key   = (s.erpnext_api_key or "").strip()
    api_secret = s.get_password("erpnext_api_secret", raise_exception=False) or ""

    if not site_url or not api_key or not api_secret:
        return None

    return {
        "site_url":               site_url,
        "api_key":                api_key,
        "api_secret":             api_secret,
        "company":                s.erpnext_company or "",
        "warehouse":              s.erpnext_default_warehouse or "",
        "price_list":             s.erpnext_selling_price_list or "Standard Selling",
        "tax_template":           s.erpnext_taxes_template or "",
        "delivery_charge_account": s.erpnext_delivery_charge_account or "",
    }


def _request(config: dict, method: str, path: str,
             payload: dict | None = None, params: dict | None = None) -> dict:
    """Authenticated request to the ERPNext REST API."""
    import requests

    url = f"{config['site_url']}{path}"
    headers = {
        "Authorization": f"token {config['api_key']}:{config['api_secret']}",
        "Content-Type":  "application/json",
    }
    try:
        resp = requests.request(
            method, url,
            json=payload,
            params=params,
            headers=headers,
            timeout=30,
        )
    except requests.RequestException as exc:
        raise ERPNextSyncError(f"Network error {method} {path}: {exc}") from exc

    if not resp.ok:
        raise ERPNextSyncError(
            f"ERPNext {method} {path} → HTTP {resp.status_code}: {resp.text[:400]}"
        )
    return resp.json() if resp.content else {}


def _erp_get(config: dict, doctype: str,
             filters: list | None = None, fields: list | None = None,
             limit: int = 1) -> list:
    params: dict = {"limit_page_length": limit}
    if filters:
        params["filters"] = json.dumps(filters)
    if fields:
        params["fields"] = json.dumps(fields)
    result = _request(config, "GET", f"/api/resource/{doctype}", params=params)
    return result.get("data") or []


def _erp_insert(config: dict, doctype: str, data: dict, submit: bool = False) -> str | None:
    """Insert a document and optionally submit it. Returns docname or None."""
    data["doctype"] = doctype
    result = _request(config, "POST", f"/api/resource/{doctype}", payload=data)
    name = (result.get("data") or {}).get("name")
    if submit and name:
        try:
            _request(config, "PUT", f"/api/resource/{doctype}/{name}",
                     payload={"docstatus": 1})
        except ERPNextSyncError as exc:
            # Submission failed — log but return the docname so the order is
            # still linked. A human can submit manually in ERPNext desk.
            frappe.log_error(f"Submit failed for {doctype} {name}: {exc}", "ERPNext Sync")
    return name


def _erp_exists(config: dict, doctype: str, filters: list) -> str | None:
    """Return the name of the first matching record, or None."""
    rows = _erp_get(config, doctype, filters=filters, fields=["name"], limit=1)
    return rows[0]["name"] if rows else None


# ── Customer sync ─────────────────────────────────────────────────────────────

def _upsert_customer(config: dict, name: str, phone: str, email: str) -> str | None:
    """Find or create a Customer in ERPNext by mobile_no. Returns docname."""
    if not phone:
        return None

    existing = _erp_exists(config, "Customer", [["mobile_no", "=", phone]])
    if existing:
        return existing

    return _erp_insert(config, "Customer", {
        "customer_name": name,
        "customer_type": "Individual",
        "mobile_no":     phone,
        "email_id":      email or "",
    })


# ── Item code resolution ──────────────────────────────────────────────────────

def _resolve_item_code(config: dict, product_name: str, product_doc) -> str | None:
    """Resolve the ERPNext item_code for a saathimart product.

    Resolution order:
      1. product_doc.item_code  (explicit mapping field on Product)
      2. product_doc.barcode    (look up Item Barcode on ERPNext side)
      3. product_doc.sku        (same barcode lookup by SKU)
      4. None → caller skips this line item (logs a warning)
    """
    # 1. Direct item_code field on Product doctype
    item_code = getattr(product_doc, "item_code", None) or ""
    if item_code:
        return item_code

    # 2. Barcode lookup on ERPNext
    barcode = getattr(product_doc, "barcode", None) or getattr(product_doc, "sku", None) or ""
    if barcode:
        rows = _erp_get(config, "Item Barcode",
                        filters=[["barcode", "=", barcode]],
                        fields=["parent"], limit=1)
        if rows:
            return rows[0]["parent"]

    # 3. Try matching by Item name (last resort — fragile but better than nothing)
    rows = _erp_get(config, "Item",
                    filters=[["item_name", "=", product_name]],
                    fields=["name"], limit=1)
    if rows:
        return rows[0]["name"]

    return None


# ── Sales Order sync ──────────────────────────────────────────────────────────

def _build_so_items(config: dict, order_items) -> tuple[list, list]:
    """Build Sales Order items list. Returns (so_items, skipped_products).

    skipped_products contains product names that could not be mapped to an
    ERPNext item_code — they are logged but do NOT abort the sync.
    """
    so_items = []
    skipped  = []

    for item in order_items:
        try:
            product_doc = frappe.get_doc("Product", item.product)
        except frappe.DoesNotExistError:
            skipped.append(item.product)
            continue

        item_code = _resolve_item_code(config, item.product_name or item.product, product_doc)
        if not item_code:
            skipped.append(item.product)
            frappe.log_error(
                f"No ERPNext item_code for product '{item.product}' "
                f"(name: {item.product_name}). Skipped in Sales Order.",
                "ERPNext Sync",
            )
            continue

        so_items.append({
            "item_code":     item_code,
            "item_name":     item.product_name or item_code,
            "qty":           flt(item.qty),
            "rate":          flt(item.rate),
            "warehouse":     config.get("warehouse", ""),
            "delivery_date": add_days(nowdate(), 1),
        })

    return so_items, skipped


def _sync_order_to_sales_order(config: dict, order) -> str:
    """Push one saathimart Order to ERPNext as a submitted Sales Order.

    Returns the ERPNext Sales Order name.
    Raises ERPNextSyncError on hard failures.
    """
    # ── 1. Customer ───────────────────────────────────────────────────────
    erp_customer = order.erpnext_customer or _upsert_customer(
        config,
        order.customer_name,
        order.customer_phone or "",
        order.customer_email or "",
    )
    if not erp_customer:
        raise ERPNextSyncError(f"Could not resolve ERPNext customer for order {order.name}")

    # Cache customer on the order row so future syncs skip the lookup
    if not order.erpnext_customer:
        frappe.db.set_value("Order", order.name, "erpnext_customer", erp_customer,
                            update_modified=False)

    # ── 2. Delivery address ───────────────────────────────────────────────
    address_name = None
    if order.delivery_address:
        existing_addr = _erp_exists(config, "Address", [
            ["Dynamic Link", "link_doctype", "=", "Customer"],
            ["Dynamic Link", "link_name",    "=", erp_customer],
            ["address_line1", "=", order.delivery_address[:140]],
        ])
        if existing_addr:
            address_name = existing_addr
        else:
            address_name = _erp_insert(config, "Address", {
                "address_title": erp_customer,
                "address_type":  "Shipping",
                "address_line1": order.delivery_address[:140],
                "city":          "Kathmandu",
                "country":       "Nepal",
                "links": [{"link_doctype": "Customer", "link_name": erp_customer}],
            })

    # ── 3. Line items ─────────────────────────────────────────────────────
    order_items = frappe.get_all(
        "Order Item",
        filters={"parent": order.name},
        fields=["product", "product_name", "qty", "rate", "amount"],
    )
    so_items, skipped = _build_so_items(config, order_items)

    if not so_items:
        raise ERPNextSyncError(
            f"Order {order.name}: all {len(order_items)} item(s) skipped — "
            "no ERPNext item_codes found. Map products first."
        )

    # ── 4. Taxes ──────────────────────────────────────────────────────────
    taxes = []
    if config.get("tax_template"):
        # Fetch the tax rows from the template so we can include delivery
        # VAT on the same row chain (same logic as saathi_middleware).
        try:
            tmpl = _request(
                config, "GET",
                f"/api/resource/Sales Taxes and Charges Template/{config['tax_template']}",
            )
            taxes = [
                {
                    "charge_type":         row["charge_type"],
                    "account_head":        row["account_head"],
                    "rate":                row["rate"],
                    "included_in_print_rate": row["included_in_print_rate"],
                    "description":         row["description"],
                }
                for row in (tmpl.get("data") or {}).get("taxes", [])
            ]
        except ERPNextSyncError:
            pass  # template fetch failed — continue without taxes

    if flt(order.delivery_charge) > 0 and config.get("delivery_charge_account"):
        taxes.append({
            "charge_type":  "Actual",
            "account_head": config["delivery_charge_account"],
            "description":  "Delivery Charge",
            "tax_amount":   flt(order.delivery_charge),
        })

    # ── 5. Discount ───────────────────────────────────────────────────────
    total_discount = flt(order.coupon_discount) + flt(order.loyalty_discount)

    # ── 6. Build payload ──────────────────────────────────────────────────
    payload: dict = {
        "customer":            erp_customer,
        "company":             config.get("company", ""),
        "selling_price_list":  config.get("price_list", "Standard Selling"),
        "delivery_date":       add_days(nowdate(), 1),
        "items":               so_items,
        "remarks":             f"Saathimart Order: {order.name}",
    }
    if address_name:
        payload["customer_address"]   = address_name
        payload["shipping_address_name"] = address_name
    if taxes:
        payload["taxes"] = taxes
    if total_discount > 0:
        payload["apply_discount_on"] = "Net Total"
        payload["discount_amount"]   = total_discount

    so_name = _erp_insert(config, "Sales Order", payload, submit=True)
    if not so_name:
        raise ERPNextSyncError(f"ERPNext did not return a Sales Order name for {order.name}")

    return so_name


def _sync_order_to_invoice(config: dict, order) -> str:
    """Create and submit a Sales Invoice against the linked Sales Order.

    For prepaid orders (eSewa etc.) creates a POS invoice with payment.
    For COD, creates a regular invoice (to be marked paid in ERPNext desk
    when the rider confirms cash collection).
    """
    so_name = order.erpnext_sales_order
    if not so_name:
        raise ERPNextSyncError(f"Order {order.name} has no erpnext_sales_order")

    # Fetch the Sales Order to map items into invoice
    so_data = _request(config, "GET", f"/api/resource/Sales Order/{so_name}")
    so = so_data.get("data") or {}
    if not so:
        raise ERPNextSyncError(f"Sales Order {so_name} not found in ERPNext")

    invoice_items = [
        {
            "item_code":   i.get("item_code"),
            "qty":         i.get("qty"),
            "rate":        i.get("rate"),
            "warehouse":   i.get("warehouse"),
            "sales_order": so_name,
            "so_detail":   i.get("name"),
        }
        for i in so.get("items", [])
    ]

    payload: dict = {
        "customer":           so.get("customer"),
        "company":            so.get("company"),
        "selling_price_list": so.get("selling_price_list"),
        "due_date":           nowdate(),
        "items":              invoice_items,
    }

    if so.get("taxes"):
        payload["taxes"] = so["taxes"]
    if so.get("discount_amount"):
        payload["apply_discount_on"] = so.get("apply_discount_on", "Net Total")
        payload["discount_amount"]   = so["discount_amount"]

    is_prepaid = (order.payment_method or "").strip().lower() not in ("cod", "cash on delivery", "")
    if is_prepaid:
        payload["is_pos"]      = 1
        payload["update_stock"] = 1
        payload["paid_amount"]  = flt(order.grand_total)
        payload["payments"] = [{
            "mode_of_payment": order.payment_method,
            "amount":          flt(order.grand_total),
        }]

    inv_name = _erp_insert(config, "Sales Invoice", payload, submit=True)
    if not inv_name:
        raise ERPNextSyncError(f"ERPNext did not return an invoice name for {order.name}")
    return inv_name


# ── Per-order sync with status tracking ──────────────────────────────────────

def _mark_order(order_name: str, status: str, error: str = "",
                sales_order: str = "", invoice: str = ""):
    """Write sync result fields back to the Order row atomically."""
    values: dict = {
        "erpnext_sync_status": status,
        "erpnext_sync_error":  error[:500] if error else "",
    }
    if status == "Synced":
        values["erpnext_synced_at"] = now_datetime()
    if sales_order:
        values["erpnext_sales_order"] = sales_order
    if invoice:
        values["erpnext_sales_invoice"] = invoice
    frappe.db.set_value("Order", order_name, values, update_modified=False)


def _sync_one_order(config: dict, order) -> bool:
    """Sync a single order: customer → Sales Order → Sales Invoice.

    Returns True on full success, False on any error.
    Writes erpnext_sync_status / erpnext_sync_error on the Order row.
    """
    try:
        # Sales Order
        if not order.erpnext_sales_order:
            so_name = _sync_order_to_sales_order(config, order)
            _mark_order(order.name, "Pending", sales_order=so_name)
        else:
            so_name = order.erpnext_sales_order

        # Sales Invoice
        if not order.erpnext_sales_invoice:
            inv_name = _sync_order_to_invoice(config, order)
        else:
            inv_name = order.erpnext_sales_invoice

        _mark_order(order.name, "Synced",
                    sales_order=so_name, invoice=inv_name)
        return True

    except Exception as exc:
        error_msg = str(exc)
        frappe.log_error(
            f"ERPNext sync failed for order {order.name}: {error_msg}",
            "ERPNext Sync",
        )
        _mark_order(order.name, "Failed", error=error_msg)
        return False


# ── Daily sync entry point ────────────────────────────────────────────────────

def run_daily_sync():
    """Daily scheduler entry point — called by hooks.py.

    Syncs all paid, non-cancelled Orders that have not yet been fully
    synced to ERPNext (erpnext_sync_status != 'Synced').

    This function is safe to call multiple times (idempotent):
    - Orders already marked Synced are skipped.
    - Failed orders from previous runs are retried.
    - If erpnext_sync_enabled is off, returns immediately.
    """
    config = _get_config()
    if not config:
        # Sync disabled or not configured — silent no-op.
        return

    # Orders eligible for sync: paid, not cancelled, not yet fully synced.
    pending = frappe.get_all(
        "Order",
        filters={
            "payment_status": "Paid",
            "status":         ["not in", ["Cancelled", "Refunded"]],
            "erpnext_sync_status": ["not in", ["Synced"]],
        },
        fields=[
            "name", "customer_name", "customer_phone", "customer_email",
            "delivery_address", "grand_total", "net_total", "total_taxes",
            "delivery_charge", "coupon_discount", "loyalty_discount",
            "payment_method", "erpnext_customer",
            "erpnext_sales_order", "erpnext_sales_invoice",
            "erpnext_sync_status",
        ],
        order_by="creation asc",
        limit=500,  # Process up to 500 per day; remainder picked up next run
    )

    synced  = 0
    failed  = 0
    skipped = 0

    for order in pending:
        ok = _sync_one_order(config, order)
        if ok:
            synced += 1
        else:
            failed += 1

    frappe.db.commit()

    frappe.logger().info(
        f"ERPNext daily sync complete: {synced} synced, {failed} failed, "
        f"{skipped} skipped out of {len(pending)} orders."
    )
    return {"synced": synced, "failed": failed, "skipped": skipped, "total": len(pending)}


# ── Whitelisted admin endpoints ───────────────────────────────────────────────

@frappe.whitelist()
def sync_single_order(order_id: str):
    """Manually push one order to ERPNext. Admin only."""
    if "SM Admin" not in frappe.get_roles():
        frappe.throw(_("Not permitted"), frappe.PermissionError)

    config = _get_config()
    if not config:
        frappe.throw(_("ERPNext sync is not configured or disabled in Settings"))

    order = frappe.get_doc("Order", order_id)
    ok = _sync_one_order(config, order)
    frappe.db.commit()

    order.reload()
    return {
        "ok":                    ok,
        "erpnext_sales_order":   order.erpnext_sales_order,
        "erpnext_sales_invoice": order.erpnext_sales_invoice,
        "erpnext_sync_status":   order.erpnext_sync_status,
        "erpnext_sync_error":    order.erpnext_sync_error,
    }


@frappe.whitelist()
def get_sync_status():
    """Return a summary of ERPNext sync status across all paid orders."""
    if "SM Admin" not in frappe.get_roles():
        frappe.throw(_("Not permitted"), frappe.PermissionError)

    rows = frappe.db.sql("""
        SELECT
            erpnext_sync_status,
            COUNT(*) AS cnt
        FROM `tabOrder`
        WHERE payment_status = 'Paid'
          AND status NOT IN ('Cancelled', 'Refunded')
        GROUP BY erpnext_sync_status
    """, as_dict=True)

    summary = {r.erpnext_sync_status or "Pending": r.cnt for r in rows}
    config  = _get_config()

    return {
        "enabled":    bool(config),
        "site_url":   (config or {}).get("site_url", ""),
        "summary":    summary,
        "total_paid": sum(summary.values()),
    }


@frappe.whitelist()
def test_connection():
    """Ping the ERPNext site to verify credentials are correct."""
    if "SM Admin" not in frappe.get_roles():
        frappe.throw(_("Not permitted"), frappe.PermissionError)

    config = _get_config()
    if not config:
        return {"connected": False, "error": "ERPNext sync not configured or disabled in Settings"}

    try:
        _request(config, "GET", "/api/method/frappe.ping")
        return {"connected": True, "site_url": config["site_url"], "company": config.get("company")}
    except ERPNextSyncError as exc:
        return {"connected": False, "error": str(exc)}
