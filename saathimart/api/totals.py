"""
calculate_taxes_and_totals — ERPNext-style totals engine for Order.

Mirrors ERPNext's calculation order exactly:
  1. Item amounts  (qty × rate)
  2. Net total     (sum of item amounts)
  3. Taxes         (each tax row computed on net_total or previous_row_total)
  4. Grand total   (net_total + total_taxes)
  4.5. Offer Coupon (resolved from offer_slug, applied before explicit coupon)
  5. Coupon        (percentage or fixed, applied on net_total)
  6. Onboarding    (zone-configured first/second-order discount, applied after coupon)
  7. Membership    (per-line, category-aware; applied after coupon + onboarding)
  8. Loyalty       (fixed discount, applied after everything above)
  9. Delivery      (added back — not discounted)
 10. Rounding      (round to nearest paisa)

Membership sits before loyalty on purpose: loyalty redemption is capped as a
percentage of what's left to pay (max_redemption_per_order_pct), so it has to
see the post-membership figure or a member could redeem points against money
they were never going to be charged.

No ERPNext import. Works purely on the Order document dict / frappe doc.
"""
from __future__ import annotations

import frappe
from frappe.utils import flt, rounded
from saathimart.api.responses import handle_api_errors


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
    _calculate_onboarding_discount(doc)
    _calculate_membership_discount(doc)
    _calculate_loyalty_discount(doc)
    _calculate_grand_total(doc)
    _round_totals(doc) ──────────────────────────────────────────────────────

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


# ── Step 4: offer coupon (resolved from offer_slug) ─────────────────────────────

def _resolve_offer_coupon(doc):
    """If an offer_slug is present, resolve the associated coupon_code and
    set it so Step 4 (Coupon) picks it up automatically."""
    offer_slug = doc.get("offer_slug") or ""
    if not offer_slug:
        return

    offer = frappe.db.get_value(
        "Offer", {"slug": offer_slug, "status": "Published", "is_active": 1},
        ["name", "coupon_code"], as_dict=True,
    )
    if offer and offer.get("coupon_code"):
        _set(doc, "offer_coupon_code", offer.coupon_code)
        if not doc.get("coupon_code"):
            _set(doc, "coupon_code", offer.coupon_code)


# ── Step 5: coupon discount ───────────────────────────────────────────────────

def _calculate_coupon_discount(doc):
    _resolve_offer_coupon(doc)
    coupon_code = doc.get("coupon_code") or ""
    if not coupon_code:
        _set(doc, "coupon_discount", 0.0)
        _set(doc, "free_delivery", 0)
        return

    try:
        from saathimart.saathimart.doctype.coupon.coupon import validate_coupon
        # Phone is passed so max_uses_per_user is actually enforced here, not
        # only at placement — otherwise the cart would show a discount the
        # order then refuses to honour.
        result = validate_coupon(
            coupon_code,
            flt(doc.get("net_total") or 0),
            doc.get("customer_phone") or None,
        )
        _set(doc, "coupon_discount", flt(result.get("discount") or 0))
        _set(doc, "free_delivery", 1 if result.get("free_delivery") else 0)
    except frappe.ValidationError:
        _set(doc, "coupon_discount", 0.0)
        _set(doc, "free_delivery", 0)


# ── Step 5: onboarding discount (location-based first/second order) ───────────

def _get_customer_order_sequence(customer_email, exclude_order_name=None):
    """
    1-indexed count of this customer's orders including the current one — 1
    for their very first order, 2 for their second, etc. Counts every prior
    Order row regardless of status (including Cancelled) so a customer
    can't reset onboarding eligibility by cancelling and re-placing.
    """
    if not customer_email:
        return 0
    filters = {"customer_email": customer_email}
    if exclude_order_name:
        filters["name"] = ["!=", exclude_order_name]
    return frappe.db.count("Order", filters) + 1


def _calculate_onboarding_discount(doc):
    """
    Auto-applied discount on a customer's first/second order, rate set per
    Delivery Zone — no coupon code needed. Location-based the same way
    earn_points()'s zone loyalty_multiplier is: the rate lives on the order's
    delivery_zone, not on the customer or a global setting, so the same
    customer's first order can be discounted differently depending on which
    zone it's delivered to.
    """
    zone_name = doc.get("delivery_zone")
    customer_email = doc.get("customer_email") or ""
    if not zone_name or not customer_email:
        _set(doc, "onboarding_discount", 0.0)
        _set(doc, "onboarding_order_sequence", 0)
        return

    zone = frappe.db.get_value(
        "Delivery Zone", zone_name,
        ["first_order_discount_pct", "second_order_discount_pct", "onboarding_max_discount_amount"],
        as_dict=True,
    )
    if not zone:
        _set(doc, "onboarding_discount", 0.0)
        _set(doc, "onboarding_order_sequence", 0)
        return

    sequence = _get_customer_order_sequence(customer_email, exclude_order_name=doc.get("name"))
    _set(doc, "onboarding_order_sequence", sequence)

    if sequence == 1:
        pct = flt(zone.first_order_discount_pct)
    elif sequence == 2:
        pct = flt(zone.second_order_discount_pct)
    else:
        pct = 0.0

    if pct <= 0:
        _set(doc, "onboarding_discount", 0.0)
        return

    net_after_coupon = flt(doc.get("net_total") or 0) - flt(doc.get("coupon_discount") or 0)
    discount = net_after_coupon * (pct / 100)
    max_discount = flt(zone.onboarding_max_discount_amount)
    if max_discount > 0:
        discount = min(discount, max_discount)

    _set(doc, "onboarding_discount", round(max(discount, 0), 2))


# ── Step 6: membership discount (per-line, category-aware) ───────────────────

def _calculate_membership_discount(doc):
    """
    Resolve the customer's membership benefits across the cart lines.

    Unlike coupon/onboarding this is line-level — see api/membership.py for
    why. It is also the only discount here that can waive delivery on its own
    (a plan perk), so it sets `free_delivery` the same way a Free Delivery
    coupon does rather than introducing a second flag for grand-total to
    check.

    Any failure resolves to zero rather than raising: a broken plan must not
    make the whole basket un-checkout-able.
    """
    customer_email = doc.get("customer_email") or ""
    items = doc.get("items") or []

    if not customer_email or not items:
        _set(doc, "membership_discount", 0.0)
        _set(doc, "membership", None)
        return

    try:
        from saathimart.api.membership import resolve_membership_discount

        # Membership applies to what is still owed after the earlier discounts,
        # not to the original net total — three stacked percentages off the same
        # base can otherwise exceed the order value.
        net_after = (
            flt(doc.get("net_total") or 0)
            - flt(doc.get("coupon_discount") or 0)
            - flt(doc.get("onboarding_discount") or 0)
        )
        result = resolve_membership_discount(items, customer_email, net_after)

        discount = min(flt(result.get("discount") or 0), max(net_after, 0))
        _set(doc, "membership_discount", rounded(discount, 2))
        _set(doc, "membership", result.get("membership"))

        if result.get("free_delivery"):
            threshold = flt(result.get("free_delivery_min_order"))
            if flt(doc.get("net_total") or 0) >= threshold:
                _set(doc, "free_delivery", 1)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Membership discount calculation failed")
        _set(doc, "membership_discount", 0.0)
        _set(doc, "membership", None)


# ── Step 7: loyalty discount ──────────────────────────────────────────────────

def _calculate_loyalty_discount(doc):
    points = flt(doc.get("loyalty_points_redeemed") or 0)
    if points <= 0:
        _set(doc, "loyalty_discount", 0.0)
        _set(doc, "loyalty_points_earned", 0.0)
        return

    customer_email = doc.get("customer_email") or ""
    net_after_discounts = (
        flt(doc.get("net_total") or 0)
        - flt(doc.get("coupon_discount") or 0)
        - flt(doc.get("onboarding_discount") or 0)
        - flt(doc.get("membership_discount") or 0)
    )

    try:
        from saathimart.api.loyalty import calculate_redemption_discount
        result = calculate_redemption_discount(customer_email, points, net_after_discounts)
        _set(doc, "loyalty_discount", flt(result.get("discount") or 0))
        _set(doc, "loyalty_points_redeemed", flt(result.get("points_used") or 0))
    except Exception:
        _set(doc, "loyalty_discount", 0.0)


# ── Step 7: grand total ───────────────────────────────────────────────────────

def _calculate_grand_total(doc):
    net_total           = flt(doc.get("net_total") or 0)
    total_taxes         = flt(doc.get("total_taxes") or 0)
    coupon_discount     = flt(doc.get("coupon_discount") or 0)
    onboarding_discount = flt(doc.get("onboarding_discount") or 0)
    membership_discount = flt(doc.get("membership_discount") or 0)
    loyalty_discount    = flt(doc.get("loyalty_discount") or 0)
    delivery_charge     = flt(doc.get("delivery_charge") or 0)
    manual_discount     = flt(doc.get("discount_amount") or 0)

    if doc.get("free_delivery"):
        delivery_charge = 0.0

    total_discount = (
        coupon_discount + onboarding_discount + membership_discount
        + loyalty_discount + manual_discount
    )
    grand_total = net_total + total_taxes - total_discount + delivery_charge
    _set(doc, "grand_total", rounded(max(grand_total, 0), 2))
    _set(doc, "total_discount", rounded(total_discount, 2))


# ── Step 8: rounding ──────────────────────────────────────────────────────────

def _round_totals(doc):
    for field in ("subtotal", "net_total", "total_taxes", "grand_total",
                  "coupon_discount", "onboarding_discount", "membership_discount",
                  "loyalty_discount", "total_discount", "delivery_charge"):
        if doc.get(field) is not None:
            _set(doc, field, rounded(flt(doc.get(field)), 2))


# ── Whitelisted preview endpoint ──────────────────────────────────────────────

@frappe.whitelist(allow_guest=True)
@handle_api_errors
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
        "delivery_zone":           delivery_zone or "",
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
            zone_multiplier = 1.0
            if delivery_zone:
                zone_multiplier = flt(frappe.db.get_value(
                    "Delivery Zone", delivery_zone, "loyalty_multiplier"
                ) or 1.0)
            earned_preview = round(
                flt(order_dict["grand_total"]) * flt(program.collection_factor) * zone_multiplier, 2
            )

    # Re-resolved (not just the total) so the cart can itemise *why* a member
    # saved — "20% off Vegetables" reads as a reason to renew; a bare number
    # does not.
    membership_breakdown = []
    if order_dict.get("membership"):
        try:
            from saathimart.api.membership import resolve_membership_discount
            membership_breakdown = resolve_membership_discount(
                resolved_items,
                order_dict["customer_email"],
                flt(order_dict["net_total"]) - flt(order_dict["coupon_discount"])
                - flt(order_dict["onboarding_discount"]),
            )["breakdown"]
        except Exception:
            membership_breakdown = []

    result = {
        "items":                        resolved_items,
        "subtotal":                     order_dict["subtotal"],
        "net_total":                    order_dict["net_total"],
        "total_taxes":                  order_dict["total_taxes"],
        "coupon_discount":              order_dict["coupon_discount"],
        "onboarding_discount":          order_dict["onboarding_discount"],
        "onboarding_order_sequence":    order_dict["onboarding_order_sequence"],
        "membership_discount":          order_dict["membership_discount"],
        "membership_breakdown":         membership_breakdown,
        "loyalty_discount":             order_dict["loyalty_discount"],
        "total_discount":               order_dict["total_discount"],
        "delivery_charge":              order_dict["delivery_charge"],
        "grand_total":                  order_dict["grand_total"],
        "loyalty_points_earned_preview": earned_preview,
        "currency":                     s.currency or "NPR",
    }
    frappe.cache().set_value(cache_key, result, expires_in_sec=120)
    return result
