"""
calculate_taxes_and_totals — ERPNext-style totals engine for Order.

Mirrors ERPNext's calculation order exactly:
  1. Item amounts  (qty × rate)
  2. Net total     (sum of item amounts)
  3. Taxes         (each tax row computed on net_total or previous_row_total)
  4. Grand total   (net_total + total_taxes)
  5. Coupon        (percentage or fixed, applied on net_total)
  6. Loyalty       (fixed discount, applied after coupon)
  7. Delivery      (added back — not discounted)
  8. Rounding      (round to nearest paisa)

No ERPNext import. Works purely on the Order document dict / frappe doc.
"""
from __future__ import annotations

import frappe
from frappe.utils import flt, rounded


def _set(obj, key, value):
    """
    Write a field on either a plain dict (preview_order_totals) or a real
    frappe.Document / child-table row (checkout's actual Order doc) — dicts
    support item assignment, Documents only support attribute assignment.
    """
    if isinstance(obj, dict):
        obj[key] = value
    else:
        setattr(obj, key, value)


# ── Main entry point ──────────────────────────────────────────────────────────

def calculate_taxes_and_totals(doc):
    """
    Recalculate all totals on an Order doc (frappe.Document or dict-like).
    Mutates the doc in place. Call before insert/save.
    """
    _calculate_item_amounts(doc)
    _calculate_net_total(doc)
    _calculate_taxes(doc)
    _calculate_coupon_discount(doc)
    _calculate_loyalty_discount(doc)
    _calculate_grand_total(doc)
    _round_totals(doc)


# ── Step 1: item amounts ──────────────────────────────────────────────────────

def _calculate_item_amounts(doc):
    for item in doc.get("items") or []:
        qty  = flt(item.get("qty") or 0)
        rate = flt(item.get("rate") or 0)
        _set(item, "amount", rounded(qty * rate, 2))


# ── Step 2: net total ─────────────────────────────────────────────────────────

def _calculate_net_total(doc):
    net_total = rounded(
        sum(flt(item.get("amount") or 0) for item in (doc.get("items") or [])), 2
    )
    _set(doc, "net_total", net_total)
    _set(doc, "subtotal", net_total)


# ── Step 3: taxes ─────────────────────────────────────────────────────────────

def _calculate_taxes(doc):
    taxes = doc.get("taxes") or []
    running_total = flt(doc.get("net_total") or 0)
    total_taxes = 0.0
    prev_row_total = 0.0

    for tax in taxes:
        charge_type = tax.get("charge_type") or "On Net Total"
        rate = flt(tax.get("rate") or 0)

        if charge_type == "Actual":
            tax_amount = flt(tax.get("tax_amount") or 0)
        elif charge_type == "On Previous Row Total":
            tax_amount = rounded(prev_row_total * rate / 100, 2)
        else:
            tax_amount = rounded(running_total * rate / 100, 2)

        _set(tax, "tax_amount", tax_amount)
        running_total += tax_amount
        prev_row_total = tax_amount
        total_taxes += tax_amount

    _set(doc, "total_taxes", rounded(total_taxes, 2))


# ── Step 4: coupon discount ───────────────────────────────────────────────────

def _calculate_coupon_discount(doc):
    coupon_code = doc.get("coupon_code") or ""
    if not coupon_code:
        _set(doc, "coupon_discount", 0.0)
        _set(doc, "free_delivery", 0)
        return

    try:
        from saathimart.saathimart.doctype.coupon.coupon import validate_coupon
        result = validate_coupon(coupon_code, flt(doc.get("net_total") or 0))
        _set(doc, "coupon_discount", flt(result.get("discount") or 0))
        _set(doc, "free_delivery", 1 if result.get("free_delivery") else 0)
    except frappe.ValidationError:
        _set(doc, "coupon_discount", 0.0)
        _set(doc, "free_delivery", 0)


# ── Step 5: loyalty discount ──────────────────────────────────────────────────

def _calculate_loyalty_discount(doc):
    points = flt(doc.get("loyalty_points_redeemed") or 0)
    if points <= 0:
        _set(doc, "loyalty_discount", 0.0)
        _set(doc, "loyalty_points_earned", 0.0)
        return

    customer_email = doc.get("customer_email") or ""
    net_after_coupon = flt(doc.get("net_total") or 0) - flt(doc.get("coupon_discount") or 0)

    try:
        from saathimart.api.loyalty import calculate_redemption_discount
        result = calculate_redemption_discount(customer_email, points, net_after_coupon)
        _set(doc, "loyalty_discount", flt(result.get("discount") or 0))
        _set(doc, "loyalty_points_redeemed", flt(result.get("points_used") or 0))
    except Exception:
        _set(doc, "loyalty_discount", 0.0)


# ── Step 6: grand total ───────────────────────────────────────────────────────

def _calculate_grand_total(doc):
    net_total        = flt(doc.get("net_total") or 0)
    total_taxes      = flt(doc.get("total_taxes") or 0)
    coupon_discount  = flt(doc.get("coupon_discount") or 0)
    loyalty_discount = flt(doc.get("loyalty_discount") or 0)
    delivery_charge  = flt(doc.get("delivery_charge") or 0)
    manual_discount  = flt(doc.get("discount_amount") or 0)

    if doc.get("free_delivery"):
        delivery_charge = 0.0

    total_discount = coupon_discount + loyalty_discount + manual_discount
    grand_total = net_total + total_taxes - total_discount + delivery_charge
    _set(doc, "grand_total", rounded(max(grand_total, 0), 2))
    _set(doc, "total_discount", rounded(total_discount, 2))


# ── Step 7: rounding ──────────────────────────────────────────────────────────

def _round_totals(doc):
    for field in ("subtotal", "net_total", "total_taxes", "grand_total",
                  "coupon_discount", "loyalty_discount", "total_discount", "delivery_charge"):
        if doc.get(field) is not None:
            _set(doc, field, rounded(flt(doc.get(field)), 2))


# ── Whitelisted preview endpoint ──────────────────────────────────────────────

@frappe.whitelist(allow_guest=True)
def preview_order_totals(items, delivery_zone=None, coupon_code=None,
                          loyalty_points=0, customer_email=None):
    """
    Frontend calls this to get a live totals preview before placing order.
    items = JSON list of {product, qty, vendor?}
    vendor on each item drives price resolution — vendor-a and vendor-b
    can return different prices for the same product.
    """
    import json, hashlib
    from saathimart.api.products import get_effective_price

    if isinstance(items, str):
        items = json.loads(items)

    cache_key = "sm_totals:" + hashlib.md5(
        json.dumps({"items": items, "delivery_zone": delivery_zone,
                    "coupon_code": coupon_code, "loyalty_points": loyalty_points,
                    "customer_email": customer_email},
                   sort_keys=True).encode()
    ).hexdigest()
    cached = frappe.cache().get_value(cache_key)
    if cached:
        return cached

    resolved_items = []
    for item in items:
        product_doc = frappe.get_doc("Product", item["product"])
        vendor = item.get("vendor") or None
        rate = get_effective_price(product_doc, vendor=vendor)
        resolved_items.append({
            "product":      product_doc.name,
            "product_name": product_doc.product_name,
            "vendor":       vendor,
            "qty":          flt(item.get("qty") or 1),
            "rate":         rate,
            "amount":       0,
        })

    delivery_charge = 0.0
    if delivery_zone:
        zone = frappe.get_doc("Delivery Zone", delivery_zone)
        if zone.is_active:
            subtotal_est = sum(i["qty"] * i["rate"] for i in resolved_items)
            delivery_charge = (
                0 if (zone.free_delivery_above and subtotal_est >= zone.free_delivery_above)
                else flt(zone.delivery_charge)
            )

    order_dict = {
        "items":                   resolved_items,
        "delivery_charge":         delivery_charge,
        "coupon_code":             coupon_code or "",
        "loyalty_points_redeemed": flt(loyalty_points),
        "customer_email":          customer_email or frappe.session.user,
        "discount_amount":         0,
        "taxes":                   [],
    }

    calculate_taxes_and_totals(order_dict)

    earned_preview = 0.0
    s = frappe.get_single("Settings")
    if s.enable_loyalty and s.loyalty_program:
        program = frappe.get_doc("Loyalty Program", s.loyalty_program)
        if program.is_active:
            earned_preview = round(
                flt(order_dict["grand_total"]) * flt(program.collection_factor), 2
            )

    result = {
        "items":                        resolved_items,
        "subtotal":                     order_dict["subtotal"],
        "net_total":                    order_dict["net_total"],
        "total_taxes":                  order_dict["total_taxes"],
        "coupon_discount":              order_dict["coupon_discount"],
        "loyalty_discount":             order_dict["loyalty_discount"],
        "total_discount":               order_dict["total_discount"],
        "delivery_charge":              order_dict["delivery_charge"],
        "grand_total":                  order_dict["grand_total"],
        "loyalty_points_earned_preview": earned_preview,
        "currency":                     s.currency or "NPR",
    }
    frappe.cache().set_value(cache_key, result, expires_in_sec=120)
    return result
