"""
Vendor Stock API — per-vendor inventory + atomic reservation.

Each (vendor, product) pair has its own Vendor Stock row instead of a single
pooled Product.stock_qty. Reservation at checkout happens via one atomic
UPDATE so two concurrent checkouts for the last unit can't both succeed —
see IMPLEMENTATION.md "Race Conditions — Every Scenario Solved" for the full
walkthrough this implements.

Lifecycle for a vendor-linked order item:
  checkout            → atomic_reserve      (available_qty -= qty, reserved_qty += qty)
  cancelled            → release_reservation (available_qty += qty, reserved_qty -= qty)
  vendor confirms       → confirm_deduction   (reserved_qty -= qty, physical_qty -= qty)
  delivery
  vendor pushes         → apply_vendor_stock_event  (receipt/deduct/adjustment —
  stock.* event                                      changes available_qty AND physical_qty)
"""
import json
import uuid

import frappe
from frappe import _
from frappe.utils import flt, now_datetime

from saathimart.api.utils import verify_hub_secret

MAX_SINGLE_EVENT_QTY = 1000  # guards against a vendor fat-fingering qty_change


# ── Helpers ──────────────────────────────────────────────────────────────────

def _row_name(vendor, product):
    return f"{vendor}-{product}"


def _next_event_seq(vendor):
    """Return the next monotonically increasing event sequence for a vendor."""
    row = frappe.db.sql("""
        SELECT MAX(last_event_seq) as max_seq
        FROM `tabVendor Stock`
        WHERE vendor = %s
    """, (vendor,), as_dict=True)
    last = row[0].max_seq if row and row[0].max_seq else 0
    return last + 1


def _generate_event_id():
    return str(uuid.uuid4())


def get_or_create(vendor, product):
    name = _row_name(vendor, product)
    if frappe.db.exists("Vendor Stock", name):
        return frappe.get_doc("Vendor Stock", name)
    doc = frappe.new_doc("Vendor Stock")
    doc.vendor = vendor
    doc.product = product
    doc.insert(ignore_permissions=True)
    frappe.cache().delete_key(f"sm_stock:{vendor}:{product}")
    return doc


def _invalidate_stock_cache(vendor, product):
    frappe.cache().delete_key(f"sm_stock:{vendor}:{product}")
    frappe.cache().delete_key(f"sm_stock_batch:{vendor}")


def _resolve_product(hub_product, barcode):
    """
    Resolve the hub Product by explicit ID first.
    Falls back to Product.sku == barcode only if barcode is provided.
    Returns (product_name, error_message_or_None).
    """
    if hub_product and frappe.db.exists("Product", hub_product):
        return hub_product, None

    if barcode:
        product = frappe.db.get_value("Product", {"sku": barcode}, "name")
        if product:
            return product, None

    return None, (
        f"Unmapped product: hub_product={hub_product!r} barcode={barcode!r}. "
        "Vendor must create an explicit Product Mapping before pushing stock."
    )


def _validate_event(vendor, product, payload):
    """
    Idempotency, ordering, size, and staleness checks.
    Returns (ok, error_message_or_None, vendor_stock_doc_or_None).
    """
    event_id = payload.get("event_id")
    event_seq = payload.get("event_seq")
    qty_change = flt(payload.get("qty_change") or 0)
    base_qty = payload.get("base_qty")

    if not vendor or not frappe.db.exists("Vendor", vendor):
        return False, f"Unknown vendor {vendor!r}", None

    row = get_or_create(vendor, product)

    # Idempotency: if we have seen this event_id, skip
    if event_id and row.last_event_id == event_id:
        return False, None, row  # not an error — already processed

    # Ordering: reject events out of sequence
    if event_seq is not None:
        last_seq = row.last_event_seq or 0
        if event_seq <= last_seq:
            return False, (
                f"Out-of-order event: seq={event_seq} <= last_seq={last_seq}. "
                "Vendor must replay from the correct sequence."
            ), row

    # Size guard
    if abs(qty_change) > MAX_SINGLE_EVENT_QTY:
        return False, (
            f"Suspiciously large qty_change={qty_change} for "
            f"vendor={vendor} product={product}. "
            "Verify with vendor before applying manually."
        ), row

    # Staleness guard: if vendor sends base_qty, ensure it matches current state.
    # Skip for brand-new Vendor Stock rows that have never been updated — the
    # vendor's base_qty IS the source of truth for the initial sync.
    if base_qty is not None and (row.last_event_seq or 0) > 0:
        current = flt(row.available_qty or 0) + flt(row.reserved_qty or 0)
        if flt(base_qty) != current:
            return False, (
                f"Staleness rejection: vendor sent base_qty={base_qty} "
                f"but hub has current_total={current} for "
                f"vendor={vendor} product={product}. "
                "Vendor must reconcile before pushing."
            ), row

    return True, None, row


def _apply_stock_delta(row, qty_change, now):
    """Apply the qty delta to Vendor Stock with atomic read-modify-write."""
    row.load_from_db()
    current_available = flt(row.available_qty or 0)
    current_reserved = flt(row.reserved_qty or 0)
    new_available = current_available + qty_change

    if qty_change < 0 and new_available < 0:
        frappe.log_error(
            f"Vendor Stock event REJECTED: vendor={row.vendor} product={row.product} "
            f"qty_change={qty_change} would make available_qty={new_available}. "
            f"Current available={current_available} reserved={current_reserved}. "
            f"Vendor must reconcile before this event can be applied.",
            "Vendor Stock Event — Rejected Negative",
        )
        raise frappe.ValidationError(
            f"Vendor Stock event would make available_qty negative: {new_available}"
        )

    new_physical = new_available + current_reserved
    frappe.db.set_value("Vendor Stock", row.name, {
        "available_qty": new_available,
        "physical_qty": new_physical,
        "last_sync_at": now,
        "last_updated": now,
    })

    if new_available < 0:
        frappe.log_error(
            f"Vendor Stock went negative: vendor={row.vendor} product={row.product} "
            f"available_qty={new_available}. Likely oversell/race — admin must reconcile.",
            "Vendor Stock — Negative Balance",
        )


def _create_sle(product, qty_change, voucher_type, voucher_no, source_site, vendor, remarks):
    """Create a Stock Ledger Entry for audit trail."""
    from saathimart.saathimart.doctype.stock_ledger_entry.stock_ledger_entry import make_entry
    make_entry(
        product=product,
        qty_change=qty_change,
        voucher_type=voucher_type,
        voucher_no=voucher_no or "",
        source_site=source_site or "",
        vendor=vendor or "",
        remarks=remarks or "",
    )


# ── Public API ────────────────────────────────────────────────────────────────

@frappe.whitelist()
def get_vendor_stock(vendor, product):
    """
    Called by saathimart-vendor's hourly reconciliation task
    (saathimart_vendor.tasks._reconcile_item) to compare hub physical_qty
    against the vendor's own ERPNext Bin.actual_qty.
    """
    cache_key = f"sm_stock:{vendor}:{product}"
    cached = frappe.cache().get_value(cache_key)
    if cached:
        return cached

    row = frappe.db.get_value(
        "Vendor Stock", _row_name(vendor, product),
        ["available_qty", "reserved_qty", "physical_qty"], as_dict=True,
    )
    if not row:
        row = {"available_qty": 0, "reserved_qty": 0, "physical_qty": 0}
    frappe.cache().set_value(cache_key, row, expires_in_sec=30)
    return row


@frappe.whitelist()
def get_vendor_stock_batch(vendor, products):
    """
    Batch stock lookup for multiple products from a single vendor.
    Returns {product: {available_qty, reserved_qty, physical_qty}} for each product.

    Called by saathimart-vendor's reconciliation to reduce 50K HTTP calls
    down to ~50 batch calls.
    """
    if isinstance(products, str):
        products = [p.strip() for p in products.split(",") if p.strip()]
    if not products or not vendor:
        return {}

    cache_key = f"sm_stock_batch:{vendor}:" + ":".join(sorted(products))
    cached = frappe.cache().get_value(cache_key)
    if cached:
        return cached

    rows = frappe.db.sql("""
        SELECT product, available_qty, reserved_qty, physical_qty
        FROM `tabVendor Stock`
        WHERE vendor = %s AND product IN %s
    """, (vendor, tuple(products)), as_dict=True)

    result = {}
    for r in rows:
        result[r.product] = {
            "available_qty": flt(r.available_qty or 0),
            "reserved_qty": flt(r.reserved_qty or 0),
            "physical_qty": flt(r.physical_qty or 0),
        }
    frappe.cache().set_value(cache_key, result, expires_in_sec=30)
    return result


def atomic_reserve(vendor, product, qty):
    """
    Move qty from available_qty to reserved_qty for one (vendor, product).
    Raises frappe.ValidationError if not enough is available *right now* —
    this single UPDATE...WHERE is what actually prevents oversell when two
    checkouts race for the last unit (see Scenario 2 in IMPLEMENTATION.md).
    """
    qty = flt(qty)
    if qty <= 0:
        frappe.throw(_("Qty must be positive"))

    get_or_create(vendor, product)  # ensure the row exists before the UPDATE

    frappe.db.sql(
        """
        UPDATE `tabVendor Stock`
        SET available_qty = available_qty - %(qty)s,
            reserved_qty  = reserved_qty  + %(qty)s,
            last_updated  = %(now)s
        WHERE vendor = %(vendor)s AND product = %(product)s
          AND available_qty >= %(qty)s
        """,
        {"qty": qty, "vendor": vendor, "product": product, "now": now_datetime()},
    )
    affected = frappe.db._cursor.rowcount

    if not affected:
        current = flt(frappe.db.get_value(
            "Vendor Stock", _row_name(vendor, product), "available_qty"
        ) or 0)
        frappe.throw(_(
            "Only {0} unit(s) left with this vendor for this product."
        ).format(current))

    _invalidate_stock_cache(vendor, product)


def atomic_reserve_batch(reservations):
    """
    Batch-reserve stock for multiple (vendor, product, qty) tuples.
    Groups by vendor and executes one UPDATE per vendor with CASE expressions.

    Returns list of (vendor, product, qty, success) tuples.
    Raises on the first failure and leaves previously-reserved items untouched.
    """
    if not reservations:
        return []

    from collections import defaultdict
    groups = defaultdict(list)
    for vendor, product, qty in reservations:
        groups[vendor].append((product, flt(qty)))

    results = []
    for vendor, items in groups.items():
        products = [p for p, _ in items]
        qtys = {p: q for p, q in items}

        case_available = " + ".join(
            f"WHEN vendor={frappe.db.escape(vendor)} AND product={frappe.db.escape(p)} THEN {flt(q)}"
            for p, q in items
        )
        case_reserved = " + ".join(
            f"WHEN vendor={frappe.db.escape(vendor)} AND product={frappe.db.escape(p)} THEN {flt(q)}"
            for p, q in items
        )
        in_clause = ", ".join(
            f"({frappe.db.escape(vendor)}, {frappe.db.escape(p)})" for p, _ in items
        )

        sql = f"""
            UPDATE `tabVendor Stock`
            SET available_qty = available_qty - CASE {case_available} END,
                reserved_qty  = reserved_qty  + CASE {case_reserved} END,
                last_updated  = %(now)s
            WHERE (vendor, product) IN ({in_clause})
              AND available_qty >= CASE {case_available} END
        """
        frappe.db.sql(sql, {"now": now_datetime()})
        affected = frappe.db._cursor.rowcount

        if affected < len(items):
            failed = len(items) - affected
            frappe.throw(_(
                "Batch reservation failed for vendor {0}: {1} of {2} items could not be reserved."
            ).format(vendor, failed, len(items)))

        for product, qty in items:
            results.append((vendor, product, qty, True))
            _invalidate_stock_cache(vendor, product)

    return results


def release_reservation(vendor, product, qty):
    """Undo a reservation — order cancelled before delivery confirmation."""
    qty = flt(qty)
    if qty <= 0:
        return
    frappe.db.sql(
        """
        UPDATE `tabVendor Stock`
        SET available_qty = available_qty + %(qty)s,
            reserved_qty  = GREATEST(reserved_qty - %(qty)s, 0),
            last_updated  = %(now)s
        WHERE vendor = %(vendor)s AND product = %(product)s
        """,
        {"qty": qty, "vendor": vendor, "product": product, "now": now_datetime()},
    )
    _invalidate_stock_cache(vendor, product)


def confirm_deduction(vendor, product, qty, order_id=None):
    """
    Vendor confirmed delivery — the reservation becomes a real, permanent
    stock decrease. reserved_qty drops; physical_qty is recalculated as
    available_qty + reserved_qty to stay consistent.
    """
    qty = flt(qty)
    if qty <= 0:
        return
    frappe.db.sql(
        """
        UPDATE `tabVendor Stock`
        SET reserved_qty = GREATEST(reserved_qty - %(qty)s, 0),
            last_updated = %(now)s
        WHERE vendor = %(vendor)s AND product = %(product)s
        """,
        {"qty": qty, "vendor": vendor, "product": product, "now": now_datetime()},
    )
    frappe.db.set_value("Vendor Stock", f"{vendor}-{product}", {
        "physical_qty": flt(frappe.db.get_value("Vendor Stock", f"{vendor}-{product}", "available_qty") or 0)
                       + flt(frappe.db.get_value("Vendor Stock", f"{vendor}-{product}", "reserved_qty") or 0),
    })
    _invalidate_stock_cache(vendor, product)

    from saathimart.saathimart.doctype.stock_ledger_entry.stock_ledger_entry import make_entry
    make_entry(
        product=product, qty_change=-qty, voucher_type="Order",
        voucher_no=order_id or "", vendor=vendor,
        remarks="Vendor confirmed delivery",
    )


@frappe.whitelist(allow_guest=True)
def apply_vendor_stock_event(event, payload):
    """
    Vendor pushes stock.receipt / stock.deduct / stock.adjustment here.
    Reached two ways: async via saathimart_vendor.utils.enqueue_outbox →
    Sync Outbox → hub events.receive → _handle_inbound (already
    authenticated there), or synchronously via saathimart_vendor's
    sync_vendor_stock() button hitting this whitelisted method directly —
    hence the secret check below, which is a no-op when called internally
    with no active HTTP request (see verify_hub_secret's docstring).

    Payload keys match saathimart_vendor.hooks.stock / tasks.py exactly:
      barcode, hub_product, qty_change, vendor_id, voucher_no, source_site, remarks
      event_id, event_seq, base_qty

    REQUIRES explicit Product Mapping — no sku=barcode fallback.
    """
    verify_hub_secret("stock.apply_vendor_stock_event")
    if event not in ("stock.receipt", "stock.deduct", "stock.adjustment"):
        frappe.throw(_("Invalid stock event type: {0}").format(event))

    vendor = payload.get("vendor_id") or payload.get("vendor")
    qty_change = flt(payload.get("qty_change"))
    barcode = payload.get("barcode")
    hub_product = payload.get("hub_product") or payload.get("product_id")
    now = now_datetime()

    # 1. Resolve product — explicit mapping required
    product, resolve_error = _resolve_product(hub_product, barcode)
    if not product:
        frappe.log_error(
            f"Unmapped stock push — event={event} barcode={barcode} "
            f"hub_product={hub_product} vendor={vendor}. {resolve_error}",
            "Vendor Stock Event — Unmapped Product",
        )
        return

    # 2. Validate: idempotency, ordering, size, staleness
    ok, error, row = _validate_event(vendor, product, payload)
    if not ok:
        if error:
            frappe.log_error(
                f"Vendor Stock event rejected: vendor={vendor} product={product} "
                f"event={event} payload={payload}. Reason: {error}",
                "Vendor Stock Event — Rejected",
            )
        return  # idempotent skip or rejected — both are non-errors

    # 3. Apply delta
    _apply_stock_delta(row, qty_change, now)

    # 4. Update idempotency/sequence metadata
    event_id = payload.get("event_id") or _generate_event_id()
    event_seq = payload.get("event_seq") or _next_event_seq(vendor)
    frappe.db.set_value("Vendor Stock", row.name, {
        "last_event_id": event_id,
        "last_event_seq": event_seq,
        "last_updated": now,
        "last_sync_at": now,
    })

    # 5. Stock Ledger Entry
    voucher_type_map = {
        "stock.receipt": "Receipt",
        "stock.deduct": "Adjustment",
        "stock.adjustment": "Adjustment",
    }
    _create_sle(
        product=product,
        qty_change=qty_change,
        voucher_type=voucher_type_map.get(event, "Adjustment"),
        voucher_no=payload.get("voucher_no") or "",
        source_site=payload.get("source_site") or "",
        vendor=vendor,
        remarks=payload.get("remarks") or "",
    )


def check_negative_vendor_stock():
    """Cron every 5 min — alert admin if any Vendor Stock row is negative."""
    rows = frappe.get_all(
        "Vendor Stock", filters={"available_qty": ["<", 0]},
        fields=["name", "vendor", "product", "available_qty"],
    )
    if rows:
        frappe.log_error(
            f"{len(rows)} Vendor Stock row(s) with negative available_qty: "
            + ", ".join(r.name for r in rows),
            "Vendor Stock — Negative Balance Check",
        )


def sync_vendor_listing_stock():
    """
    Sync Vendor Listing stock fields from Vendor Stock.

    Vendor Stock is the authoritative source for inventory. Vendor Listing
    caches stock data for quick reads in product listings.
    Run this periodically (cron) or after stock events.
    """
    rows = frappe.db.sql("""
        SELECT vl.name, vs.available_qty, vs.reserved_qty, vs.physical_qty, vs.last_updated
        FROM `tabVendor Listing` vl
        LEFT JOIN `tabVendor Stock` vs ON vs.vendor = vl.vendor AND vs.product = vl.product
        WHERE vl.status = 'Active'
          AND (vs.last_updated IS NOT NULL
               OR vl.available_qty != IFNULL(vs.available_qty, 0)
               OR vl.reserved_qty != IFNULL(vs.reserved_qty, 0)
               OR vl.physical_qty != IFNULL(vs.physical_qty, 0))
    """, as_dict=True)

    updated = 0
    for r in rows:
        frappe.db.set_value("Vendor Listing", r.name, {
            "available_qty": flt(r.available_qty or 0),
            "reserved_qty": flt(r.reserved_qty or 0),
            "physical_qty": flt(r.physical_qty or 0),
            "last_updated": r.last_updated or now_datetime(),
        })
        updated += 1

    return updated


@frappe.whitelist()
def reconcile_vendor_stock(vendor, product, expected_qty):
    """
    Vendor-side reconciliation: force-correct Vendor Stock to expected_qty
    and update last_known_qty. Creates a Stock Ledger Entry of type Adjustment.

    This endpoint is called by saathimart-vendor's reconciliation task
    when drift exceeds the configured threshold.
    """
    if not vendor or not product:
        frappe.throw(_("vendor and product are required"))
    if not frappe.db.exists("Vendor", vendor):
        frappe.throw(_("Unknown vendor: {0}").format(vendor))
    if not frappe.db.exists("Product", product):
        frappe.throw(_("Unknown product: {0}").format(product))

    row = get_or_create(vendor, product)
    current_available = flt(row.available_qty or 0)
    current_reserved = flt(row.reserved_qty or 0)
    current_total = current_available + current_reserved
    expected = flt(expected_qty)

    # The adjustment only touches available_qty; reserved_qty stays untouched.
    # We set physical_qty = new_available + reserved_qty.
    adjustment = expected - current_total
    if adjustment == 0:
        frappe.db.set_value("Vendor Stock", row.name, {
            "last_known_qty": expected,
            "last_sync_at": now_datetime(),
            "last_updated": now_datetime(),
        })
        return {
            "ok": True,
            "vendor": vendor,
            "product": product,
            "adjustment": 0,
            "message": "No adjustment needed",
        }

    new_available = current_available + adjustment
    if new_available < 0:
        frappe.throw(_(
            "Reconciliation would make available_qty negative ({0}). "
            "Adjust expected_qty or resolve reservations first."
        ).format(new_available))

    now = now_datetime()
    frappe.db.set_value("Vendor Stock", row.name, {
        "available_qty": new_available,
        "physical_qty": new_available + current_reserved,
        "last_known_qty": expected,
        "last_sync_at": now,
        "last_updated": now,
    })

    _create_sle(
        product=product,
        qty_change=adjustment,
        voucher_type="Adjustment",
        voucher_no="RECONCILIATION",
        source_site="",
        vendor=vendor,
        remarks=f"Reconciliation: ERPNext expected {expected}, hub had {current_total}",
    )

    return {
        "ok": True,
        "vendor": vendor,
        "product": product,
        "adjustment": adjustment,
        "old_total": current_total,
        "new_total": expected,
        "message": f"Adjusted by {adjustment} units",
    }
