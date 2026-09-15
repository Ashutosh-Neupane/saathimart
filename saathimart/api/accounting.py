"""
Three-Party Marketplace Clearing House Accounting Engine

Handles all accounting for the SaathiMart ecosystem:
  - SaathiMart Platform (Entity A): Marketplace operator, platform coupons, loyalty
  - SaathiMart Vendor (Entity B): Independent merchant, product VAT, store coupons
  - Logistics Partner (Entity C): Delivery service, shipping VAT

Every transaction produces proper double-entry postings so that:
  1. Each entity's PAN/VAT is tracked separately
  2. Coupon discounts are attributed to the correct party
  3. Loyalty point redemptions are reimbursed from platform to vendor
  4. Delivery charges flow through a separate logistics ledger
  5. Settlement Journal Entries clear clearing accounts

All accounting is against SM Order (our custom doctype), NOT ERPNext Sales Order.

Entity A (this platform) has no GL Entry doctype of its own to write to —
this hub runs plain Frappe with no ERPNext (see README: "no ERPNext
dependency"), so it cannot hold ledger rows locally. What this module
computes — which accounts, which amounts, per-vendor commission, coupon
absorption, VAT — is still entirely the hub's job, since it's the only
place with the cross-vendor data (Order, Vendor Fulfillment, Coupon) to
compute it from. The actual GL Entry rows are created on the Platform
Ledger Vendor's site instead (SaathiMart Settings > Platform Ledger Vendor
— see api.commission.get_platform_ledger_vendor): every function below
that used to call create_gl_entries_batch() now calls
events.publisher.publish_platform_ledger_entry() with the same computed
entries, keyed by symbolic PLATFORM_ACCOUNTS names instead of resolved
account names, and that vendor's site resolves and creates them for real —
the same event-push pattern already used for settlement.completed and
order.new. (Previously this module tried to create GL Entry rows against a
doctype that never existed on this site at all — see the git history for
the incident.)
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, rounded

from saathimart.api.commission import get_commission_pct_for_vendor
from saathimart.events.publisher import publish_platform_ledger_entry


# ── Chart of Accounts Structure ──────────────────────────────────────────────
# These accounts MUST exist in the ERPNext Chart of Accounts for the platform.
# Created via a setup script or manually in ERPNext desk.

PLATFORM_ACCOUNTS = {
    "cash_bank":           "Cash/Bank - SM",
    "revenue":             "Product Revenue - SM",
    "commission_income":   "Marketplace Commission - SM",
    "platform_coupon_exp": "Platform Coupon Expense - SM",
    "loyalty_expense":     "Loyalty Points Expense - SM",
    "delivery_income":     "Delivery Service Income - SM",
    "clearing_vendor":     "Clearing Account - Vendor - SM",
    "clearing_logistics":  "Clearing Account - Logistics - SM",
    "tds_receivable":      "TDS Receivable - SM",
    "tds_payable":         "TDS Payable - SM",
    "platform_coupon_payable": "Platform Coupon Payable - SM",
    "loyalty_payable":         "Loyalty Payable - SM",
    "vat_output":          "Output VAT - SM",
    "vat_input":           "Input VAT - SM",
    "accounts_receivable": "Accounts Receivable - SM",
    "accounts_payable":    "Accounts Payable - SM",
}

# ── Order Payment GL Entries ─────────────────────────────────────────────────
# Called after payment is confirmed (eSewa callback / COD delivery)

def _compute_commission_and_platform_entries(order, amount):
    """The platform's own (Entity A) ledger entries for a customer's order
    payment. Shared by record_order_payment_gl (forward, on payment) and
    create_refund_gl_entries (reversed, on refund) so a refund always
    exactly mirrors whatever was actually booked — a second, hand-written
    copy of this math for the refund path is exactly how it drifted out of
    sync with the real model before (see create_refund_gl_entries).

    Delivery is deliberately NOT booked here — record_delivery_charge_gl
    (Entity C's logistics clearing) is the only place delivery income is
    recorded; on_payment_log_created used to call both this function's old
    body *and* that one for the same order, double-booking delivery income
    and its VAT under two different account treatments. This still
    subtracts delivery_charge from the vendor-clearing residual below
    (that slice of cash isn't the vendor's either way) — it just no longer
    books its own income/VAT rows for it a second time.

    ── Nepal marketplace model: the platform's PAN books only ITS OWN
    income and liabilities. Product revenue and product Output VAT belong
    to the VENDOR's PAN (they made the sale) — booking them here would
    declare the vendors' sales on the platform's VAT return. What the
    platform actually earns on this order:
      - marketplace commission (a service -> 13% service VAT)
      - nothing else (delivery is Entity C's, coupons/loyalty are costs)
    Everything else the customer paid sits in the vendor Clearing Account
    until settlement.
    """
    entries = []

    # ── Per-vendor commission (each vendor has their own rate) ──
    fulfillments = frappe.get_all(
        "Vendor Fulfillment",
        filters={"parent": order.name, "status": ["!=", "Cancelled"]},
        fields=["vendor", "subtotal"],
    )
    total_subtotal = sum(flt(f.subtotal) for f in fulfillments) or 1.0

    coupon_absorption = _get_coupon_absorption(order)
    platform_absorbs = flt(coupon_absorption.get("platform_absorbs", 0))
    loyalty = flt(order.loyalty_discount or 0)
    delivery_charge = flt(order.delivery_charge or 0)

    commission_total = 0.0
    commission_detail = []
    for f in fulfillments:
        # Vendor-absorbed coupons reduce this vendor's base proportionally
        vendor_gross = flt(f.subtotal) - (
            flt(coupon_absorption.get("vendor_absorbs", 0)) * flt(f.subtotal) / total_subtotal
        )
        pct = get_commission_pct_for_vendor(f.vendor) if f.vendor else 0
        commission_v = rounded(vendor_gross * pct / 100.0, 2)
        if commission_v > 0:
            commission_detail.append(f"{f.vendor}: {vendor_gross}x{pct}%={commission_v}")
        commission_total += commission_v

    service_vat = rounded(commission_total * 13.0 / 100.0, 2)

    # ── Step 1: Cash/Bank debit (the whole bill the customer paid) ──
    entries.append({
        "account_key": "cash_bank",
        "debit": flt(amount, 2),
        "credit": 0,
        "party_type": "Customer",
        "party": order.customer_email or order.customer_name,
    })

    # ── Step 2: Platform Coupon Expense (platform-funded discount) ──
    if platform_absorbs > 0:
        entries.append({
            "account_key": "platform_coupon_exp",
            "debit": platform_absorbs,
            "credit": 0,
            "remarks": f"Platform coupon absorbed for {order.name}",
        })

    # ── Step 3: Loyalty Expense (platform reimburses vendor) ──
    if loyalty > 0:
        entries.append({
            "account_key": "loyalty_expense",
            "debit": loyalty,
            "credit": 0,
            "remarks": f"Loyalty points redeemed for {order.name}",
        })

    # ── Step 4: Marketplace Commission Income (platform's service fee) ──
    if commission_total > 0:
        entries.append({
            "account_key": "commission_income",
            "debit": 0,
            "credit": commission_total,
            "remarks": f"Commission for {order.name} ({'; '.join(commission_detail)})",
        })

    # ── Step 5: Service VAT on commission (13% — platform's own VAT liability) ──
    if service_vat > 0:
        entries.append({
            "account_key": "vat_output",
            "debit": 0,
            "credit": service_vat,
            "remarks": f"Service VAT on commission for {order.name}",
        })

    # ── Step 6: Vendor Clearing (residual = everything owed to vendors) ──
    # Balancing figure: cash-in + platform-funded discounts − platform's own
    # earnings − the delivery slice (Entity C's, not the vendor's). Settlement
    # claims per-vendor amounts out of this account.
    clearing_amount = rounded(
        flt(amount) + platform_absorbs + loyalty
        - commission_total - service_vat - delivery_charge,
        2,
    )
    if clearing_amount > 0:
        entries.append({
            "account_key": "clearing_vendor",
            "debit": 0,
            "credit": clearing_amount,
            "remarks": f"Vendor clearing for {order.name}",
        })
    elif clearing_amount < 0:
        frappe.log_error(
            f"Negative vendor clearing {clearing_amount} for {order.name} — "
            "commission + VAT + delivery exceed the bill; check commission_pct",
            "Accounting",
        )

    return entries


def record_order_payment_gl(order_id, amount, gateway="", reference=""):
    """
    Push the platform's ledger entries for a customer's order payment to
    the Platform Ledger Vendor (see module docstring and
    _compute_commission_and_platform_entries for what gets booked and why).

    Idempotency is the receiving vendor's job now (its create_gl_entry
    already dedupes on account+voucher+amount+remarks) — this function no
    longer checks locally, since the hub has no GL Entry doctype to check
    against.
    """
    order = frappe.get_doc("Order", order_id)
    if not order:
        return

    entries = _compute_commission_and_platform_entries(order, amount)
    publish_platform_ledger_entry(
        voucher_type="Payment Entry",
        voucher_no=order_id,
        entries=entries,
        remarks=f"Payment received for order {order_id} via {gateway}",
        event_suffix="payment",
    )


def _get_coupon_absorption(order):
    """
    Determine who absorbs the coupon discount:
      - Vendor Coupon: Vendor absorbs (reduces their taxable base)
      - Platform Coupon: Platform absorbs (reimburses vendor)
    
    Returns dict with vendor_absorbs and platform_absorbs amounts.
    """
    coupon_code = order.get("coupon_code") or ""
    coupon_discount = flt(order.get("coupon_discount") or 0)

    if not coupon_code or coupon_discount <= 0:
        return {"vendor_absorbs": 0, "platform_absorbs": 0, "type": "none"}

    # Check coupon type
    coupon_doc = frappe.db.get_value(
        "Coupon",
        {"coupon_code": coupon_code},
        ["name", "coupon_type", "absorption_type"],
        as_dict=True,
    )

    if not coupon_doc:
        return {"vendor_absorbs": 0, "platform_absorbs": 0, "type": "unknown"}

    # absorption_type field: "Vendor" or "Platform" (default: Platform)
    absorption = getattr(coupon_doc, "absorption_type", "Platform") or "Platform"

    if absorption == "Vendor":
        return {"vendor_absorbs": coupon_discount, "platform_absorbs": 0, "type": "vendor"}
    else:
        return {"vendor_absorbs": 0, "platform_absorbs": coupon_discount, "type": "platform"}


def _calculate_vendor_clearing_amount(order, coupon_absorption):
    """
    Calculate the net amount owed to vendors after all deductions.
    
    Vendor gets:
      - Their fulfilled subtotal
      - MINUS vendor-absorbed coupons
      - MINUS platform commission
      - PLUS platform reimbursement for loyalty/platform coupons
    """
    total_vendor_subtotal = 0
    for fulfillment in frappe.get_all(
        "Vendor Fulfillment",
        filters={"parent": order.name, "status": ["!=", "Cancelled"]},
        fields=["subtotal", "vendor"],
    ):
        total_vendor_subtotal += flt(fulfillment.subtotal)

    # Vendor absorbs their own coupons
    vendor_coupon_absorption = coupon_absorption.get("vendor_absorbs", 0)

    # Platform reimburses for platform coupons + loyalty
    platform_reimbursement = (
        coupon_absorption.get("platform_absorbs", 0) +
        flt(order.loyalty_discount or 0)
    )

    # Commission is on the vendor's gross sales minus their coupons
    commission_pct = 0
    for vendor_name in set(f.subtotal and frappe.db.get_value(
        "Vendor Fulfillment", f.name, "vendor"
    ) for f in frappe.get_all(
        "Vendor Fulfillment",
        filters={"parent": order.name, "status": ["!=", "Cancelled"]},
        fields=["vendor"],
    )):
        if vendor_name:
            pct = get_commission_pct_for_vendor(vendor_name)
            commission_pct = max(commission_pct, pct)  # Use highest commission

    vendor_gross = total_vendor_subtotal - vendor_coupon_absorption
    commission = vendor_gross * commission_pct / 100
    net_to_vendor = vendor_gross - commission + platform_reimbursement

    return max(net_to_vendor, 0)


# ── Loyalty Reimbursement GL Entries ─────────────────────────────────────────

def record_loyalty_reimbursement_gl(order_id):
    """
    When loyalty points are redeemed, the platform must reimburse the vendor.
    Creates a Credit Note (Debit Note) from Vendor to Platform.
    
    Vendor View: Treated as cash received from Saathimart.
    Platform View: An expense for user retention.
    """
    order = frappe.get_doc("Order", order_id)
    if not order or not flt(order.loyalty_discount):
        return

    loyalty_amount = flt(order.loyalty_discount)

    # Platform debits loyalty expense, credits clearing account
    entries = [
        {
            "account_key": "loyalty_expense",
            "debit": loyalty_amount,
            "credit": 0,
            "remarks": f"Loyalty reimbursement for {order_id}",
        },
        {
            "account_key": "clearing_vendor",
            "debit": 0,
            "credit": loyalty_amount,
            "remarks": f"Loyalty reimbursement for {order_id}",
        },
    ]

    publish_platform_ledger_entry(
        voucher_type="Journal Entry",
        voucher_no=order_id,
        entries=entries,
        remarks=f"Loyalty points reimbursement for order {order_id}",
        event_suffix="loyalty",
    )


# ── Delivery Charge GL Entries ───────────────────────────────────────────────

def _compute_delivery_entries(order):
    """Entity C (logistics) ledger entries for this order's delivery charge.
    Shared by record_delivery_charge_gl (forward) and create_refund_gl_entries
    (reversed) — same reason as _compute_commission_and_platform_entries.

    This is the ONLY place delivery income is booked — deliberately not
    also booked inside _compute_commission_and_platform_entries, which
    used to duplicate this under a different account treatment (see git
    history). Delivery charges flow through a separate logistics entity
    ledger; the vendor's PAN must NOT include them.
    """
    delivery_amount = flt(order.delivery_charge or 0)
    if delivery_amount <= 0:
        return []

    # The customer's delivery charge is VAT-INCLUSIVE (13% is inside what
    # they paid — same model as the ERPNext middleware, VAT Act s.12).
    # Back it out: VAT = amount × 13/113, income = amount × 100/113.
    delivery_vat = rounded(delivery_amount * 13.0 / 113.0, 2)

    # Debit clearing (logistics owes this), credit delivery income
    entries = [
        {
            "account_key": "clearing_logistics",
            "debit": delivery_amount,
            "credit": 0,
            "remarks": f"Delivery charge for {order.name}",
        },
        {
            "account_key": "delivery_income",
            "debit": 0,
            "credit": delivery_amount - delivery_vat,
            "remarks": f"Delivery service income for {order.name}",
        },
    ]
    if delivery_vat > 0:
        entries.append({
            "account_key": "vat_output",
            "debit": 0,
            "credit": delivery_vat,
            "remarks": f"Delivery VAT for {order.name}",
        })
    return entries


def record_delivery_charge_gl(order_id):
    """Push this order's delivery-charge ledger entries (see
    _compute_delivery_entries) to the Platform Ledger Vendor."""
    order = frappe.get_doc("Order", order_id)
    if not order:
        return

    entries = _compute_delivery_entries(order)
    if not entries:
        return

    publish_platform_ledger_entry(
        voucher_type="Payment Entry",
        voucher_no=order_id,
        entries=entries,
        remarks=f"Delivery charge accounting for order {order_id}",
        event_suffix="delivery",
    )


# ── Vendor Settlement Journal Entry ──────────────────────────────────────────
#
# _get_party_balance (a direct `tabGL Entry` query, since removed) used to
# bound the promotional payables below against their actual outstanding
# balance. That table lives on the Platform Ledger Vendor's site now, not
# here — see create_settlement_journal_entry's docstring.


def create_settlement_journal_entry(vendor_name, payout_id, amount, commission,
                                    coupon_reimbursement=0, loyalty_reimbursement=0,
                                    tds_amount=0):
    """
    Create Journal Entry when settling with vendor (weekly/bi-weekly payout).

    Commission income and its service VAT were already recognised when the
    customer's payment was booked (record_order_payment_gl) — this entry
    only moves money:

      DR: Clearing Account - Vendor    (relieves the payable)
      DR: Platform Coupon Payable      (promotional funding reclassified
      DR: Loyalty Payable               out of clearing by the nightly job)
      CR: Bank                         (cash actually transferred to vendor)
      CR: TDS Payable                  (vendor withheld s88 TDS on the
                                         commission; held in suspense until
                                         the TDS certificate reconciles)

    Balanced by construction: DR = amount + tds regardless of how much of
    the promotional payables exist (bounded clearing picks up the rest).

    `commission`, `coupon_reimbursement` and `loyalty_reimbursement` are
    accepted for backward compatibility with earlier callers.

    The promotional payables used to be capped against
    _get_party_balance(account, vendor_name) — a direct query against this
    site's own `tabGL Entry` for what's actually still outstanding. That
    table lives on the Platform Ledger Vendor's site now, not here, so the
    hub can no longer verify the bound itself; the caller-supplied
    coupon_reimbursement/loyalty_reimbursement amounts are trusted as-is
    (payouts.py derives them from generate_settlement_statement, which is
    the authoritative source for what's actually due).
    """
    tds = flt(tds_amount)
    promo_coupon = rounded(flt(coupon_reimbursement), 2)
    promo_loyalty = rounded(flt(loyalty_reimbursement), 2)
    relieved = flt(amount) + tds

    # ── Debits: relieve the liabilities we owe the vendor ──
    entries = []
    if promo_coupon > 0:
        entries.append({
            "account_key": "platform_coupon_payable",
            "debit": promo_coupon,
            "credit": 0,
            "party_type": "Supplier",
            "party": vendor_name,
            "remarks": f"Platform coupon funding cleared for {vendor_name} — payout {payout_id}",
        })

    if promo_loyalty > 0:
        entries.append({
            "account_key": "loyalty_payable",
            "debit": promo_loyalty,
            "credit": 0,
            "party_type": "Supplier",
            "party": vendor_name,
            "remarks": f"Loyalty funding cleared for {vendor_name} — payout {payout_id}",
        })

    entries.append({
        "account_key": "clearing_vendor",
        "debit": rounded(relieved - promo_coupon - promo_loyalty, 2),
        "credit": 0,
        "party_type": "Supplier",
        "party": vendor_name,
        "remarks": f"Clearing for {vendor_name} payout {payout_id}",
    })

    # ── Credits: cash leaves, withheld TDS sits in suspense ──
    entries.append({
        "account_key": "cash_bank",
        "debit": 0,
        "credit": flt(amount, 2),
        "party_type": "Supplier",
        "party": vendor_name,
        "remarks": f"Payout to {vendor_name} for {payout_id}",
    })

    if tds > 0:
        entries.append({
            "account_key": "tds_payable",
            "debit": 0,
            "credit": tds,
            "party_type": "Supplier",
            "party": vendor_name,
            "remarks": f"TDS withheld by {vendor_name} on commission (s88) — payout {payout_id}",
        })

    publish_platform_ledger_entry(
        voucher_type="Journal Entry",
        voucher_no=payout_id,
        entries=entries,
        remarks=f"Settlement for {vendor_name} ({payout_id})",
        event_suffix="settlement",
    )


# create_loyalty_credit_note (dead code, no callers, removed here) resolved
# "clearing_platform"/"loyalty_income" — VENDOR accounts — against the
# hub's own chart, which never made sense (the hub doesn't hold the
# vendor's books). record_loyalty_reimbursement_gl above already does the
# real equivalent correctly, from the platform side, via the event push.


# ── Vendor Settlement Statement ──────────────────────────────────────────────

def generate_settlement_statement(vendor_name, from_date, to_date):
    """
    Generate a detailed vendor settlement statement showing:
      - Total sales
      - Platform commission
      - Vendor coupon discounts
      - Platform coupon reimbursements
      - Loyalty reimbursements
      - Returns/adjustments
      - Net payout due
    """
    # Get all fulfillments in period
    fulfillments = frappe.db.sql("""
        SELECT vf.name, vf.subtotal, vf.status, vf.vendor_payout,
               o.name as order_id, o.customer_name, o.coupon_code,
               o.coupon_discount, o.loyalty_discount, o.payment_status,
               vf.modified as delivered_at
        FROM `tabVendor Fulfillment` vf
        INNER JOIN `tabOrder` o ON vf.parent = o.name
        WHERE vf.vendor = %s
          AND vf.status != 'Cancelled'
          AND o.payment_status = 'Paid'
          AND o.status != 'Cancelled'
          AND DATE(vf.modified) BETWEEN %s AND %s
    """, (vendor_name, from_date, to_date), as_dict=True)

    # Platform-wide commission rate (SaathiMart Settings) — commission is
    # charged by the platform to every vendor, not stored per vendor.
    commission_pct = get_commission_pct_for_vendor(vendor_name)

    # Calculate totals
    total_sales = sum(flt(f.subtotal) for f in fulfillments)
    total_coupon_discount = sum(flt(f.coupon_discount) for f in fulfillments)
    total_loyalty_discount = sum(flt(f.loyalty_discount) for f in fulfillments)

    # Determine coupon absorption
    vendor_coupon_absorbed = 0
    platform_coupon_absorbed = 0
    for f in fulfillments:
        if f.coupon_code and flt(f.coupon_discount) > 0:
            absorption = _get_coupon_absorption_by_code(f.coupon_code)
            if absorption == "Vendor":
                vendor_coupon_absorbed += flt(f.coupon_discount)
            else:
                platform_coupon_absorbed += flt(f.coupon_discount)

    # Commission calculation
    commission_base = total_sales - vendor_coupon_absorbed
    commission_amount = commission_base * commission_pct / 100

    # Net payout
    net_payout = (
        commission_base
        - commission_amount
        + platform_coupon_absorbed  # Platform reimburses vendor
        + total_loyalty_discount    # Platform reimburses vendor for loyalty
    )

    # Already paid
    already_paid = sum(
        flt(f.subtotal) for f in fulfillments
        if f.vendor_payout and f.vendor_payout.strip()
    )

    payout_due = net_payout - already_paid

    return {
        "vendor": vendor_name,
        "period": {"from": from_date, "to": to_date},
        "total_sales": round(total_sales, 2),
        "vendor_coupon_discount": round(vendor_coupon_absorbed, 2),
        "platform_coupon_discount": round(platform_coupon_absorbed, 2),
        "loyalty_discount": round(total_loyalty_discount, 2),
        "commission_pct": commission_pct,
        "commission_base": round(commission_base, 2),
        "commission_amount": round(commission_amount, 2),
        "platform_reimbursement": round(platform_coupon_absorbed + total_loyalty_discount, 2),
        "net_payout": round(net_payout, 2),
        "already_paid": round(already_paid, 2),
        "payout_due": round(payout_due, 2),
        "order_count": len(fulfillments),
    }


def _get_coupon_absorption_by_code(coupon_code):
    """Check if a coupon is vendor-absorbed or platform-absorbed."""
    if not coupon_code:
        return "Platform"
    absorption = frappe.db.get_value(
        "Coupon",
        {"coupon_code": coupon_code},
        "absorption_type",
    )
    return absorption or "Platform"


# ── Whitelisted API Endpoints ────────────────────────────────────────────────

@frappe.whitelist()
def get_settlement_statement(vendor, from_date, to_date):
    """API endpoint to get vendor settlement statement."""
    if "SM Admin" not in frappe.get_roles():
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    return generate_settlement_statement(vendor, from_date, to_date)


# ── Hook Handlers ──────────────────────────────────────────────────────────────

def on_payment_log_created(doc, method):
    """Hook: called after Payment Log is created. Triggers GL Entry generation."""
    if doc.status != "Success":
        return
    try:
        # Create GL Entries for the order payment
        record_order_payment_gl(
            doc.order,
            doc.amount,
            gateway=doc.gateway or "",
            reference=doc.reference or "",
        )
        # Create loyalty reimbursement GL entries if loyalty was used
        order = frappe.get_doc("Order", doc.order)
        if flt(order.loyalty_discount) > 0:
            record_loyalty_reimbursement_gl(doc.order)
        # Create delivery charge GL entries if delivery was charged
        if flt(order.delivery_charge) > 0:
            record_delivery_charge_gl(doc.order)
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            f"Accounting GL Entry failed for order {doc.order}"
        )


@frappe.whitelist()
def get_gl_entries_for_order(order_id):
    """GL Entries for an order live on the Platform Ledger Vendor's site now
    (see module docstring) — this hub has no GL Entry doctype to read from
    at all. Call saathimart_vendor.api.vendor_accounting.get_vendor_gl_entries
    on that vendor's site (SaathiMart Settings > Platform Ledger Vendor)
    instead of this endpoint.
    """
    frappe.throw(
        _(
            "GL Entries are not stored on this site. Query "
            "get_vendor_gl_entries on the configured Platform Ledger "
            "Vendor's own site instead."
        ),
        frappe.ValidationError,
    )


# ── Refund GL Entries ───────────────────────────────────────────────────────
def create_refund_gl_entries(order_id, refund_amount, reason=""):
    """
    Push reversal ledger entries when a paid order is refunded, to the
    Platform Ledger Vendor (see module docstring).

    Reverses whatever record_order_payment_gl / record_delivery_charge_gl
    actually booked for this order — via the same shared helpers
    (_compute_commission_and_platform_entries, _compute_delivery_entries),
    debit and credit swapped — instead of the old hand-written reversal
    that touched "cash_bank"/"revenue", accounts the payment path never
    even books under this model (see git history). Commission, service
    VAT, coupon/loyalty expense, delivery income and the vendor-clearing
    residual are all reversed now, not just a generic cash/revenue pair.

    Assumes a full refund of the order as originally paid: `refund_amount`
    feeds the same computation record_order_payment_gl used, so a partial
    refund (less than the full order) would still reverse the *full*
    commission/coupon/loyalty/delivery figures, not a proportional slice.
    A proportional partial refund would need the caller to pass through a
    fraction or a recomputed breakdown — out of scope for this pass.

    The vendor-side clearing entries are NOT touched here — the vendor
    handles that when it receives the order.cancelled event.
    """
    order = frappe.get_doc("Order", order_id)
    if not order:
        return

    forward_entries = _compute_commission_and_platform_entries(order, refund_amount)
    forward_entries += _compute_delivery_entries(order)

    reversed_entries = [
        {
            **entry,
            "debit": entry.get("credit", 0),
            "credit": entry.get("debit", 0),
            "remarks": f"Refund reversal ({reason}): {entry.get('remarks', '')}".strip(),
        }
        for entry in forward_entries
    ]

    publish_platform_ledger_entry(
        voucher_type="Journal Entry",
        voucher_no=order_id,
        entries=reversed_entries,
        remarks=f"Refund for order {order_id}: {reason}",
        event_suffix="refund",
    )

