"""
Three-Party Marketplace Clearing House Accounting Engine

Handles all accounting for the SaathiMart ecosystem:
  - SaathiMart Platform (Entity A): Marketplace operator, platform coupons, loyalty
  - SaathiMart Vendor (Entity B): Independent merchant, product VAT, store coupons
  - Logistics Partner (Entity C): Delivery service, shipping VAT

Every transaction creates proper double-entry GL Entries so that:
  1. Each entity's PAN/VAT is tracked separately
  2. Coupon discounts are attributed to the correct party
  3. Loyalty point redemptions are reimbursed from platform to vendor
  4. Delivery charges flow through a separate logistics ledger
  5. Settlement Journal Entries clear clearing accounts

All accounting is against SM Order (our custom doctype), NOT ERPNext Sales Order.
ERPNext GL Entries are created for audit trail and tax compliance.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, nowdate, getdate, rounded

from saathimart.api.commission import get_commission_pct_for_vendor


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

VENDOR_ACCOUNTS = {
    "cash_bank":              "Cash/Bank - Vendor",
    "revenue":                "Product Revenue - Vendor",
    "vat_output":             "Output VAT - Vendor",
    "vat_input":              "Input VAT - Vendor",
    "clearing_platform":      "Clearing Account - Platform - Vendor",
    "commission_expense":     "Marketplace Commission Expense - Vendor",
    "platform_coupon_income": "Platform Coupon Reimbursement - Vendor",
    "loyalty_income":         "Loyalty Reimbursement - Vendor",
}

# Fuzzy keyword fallbacks — used when the exact account name from the map
# above is not found (e.g. company abbreviation differs, or setup_accounts
# was never run). Mirrors the same pattern in vendor_accounting.py so both
# sides behave consistently. Each list is tried in order; first non-group
# leaf match wins.
_PLATFORM_FUZZY = {
    "cash_bank":           ["Cash/Bank", "Cash In Hand", "Cash"],
    "revenue":             ["Product Revenue", "Sales", "Revenue"],
    "commission_income":   ["Marketplace Commission", "Commission on Sales", "Indirect Income"],
    "platform_coupon_exp": ["Platform Coupon", "Coupon Expense", "Indirect Expenses"],
    "loyalty_expense":     ["Loyalty", "Indirect Expenses"],
    "delivery_income":     ["Delivery Service", "Delivery Charges", "Indirect Income"],
    "clearing_vendor":     ["Clearing Account - Vendor", "Clearing - Vendor", "Accounts Payable"],
    "clearing_logistics":  ["Clearing Account - Logistics", "Clearing - Logistics", "Accounts Payable"],
    "tds_receivable":      ["TDS Receivable", "TDS", "Advance Tax", "Duties and Taxes"],
    "tds_payable":         ["TDS Payable", "TDS", "Duties and Taxes"],
    "platform_coupon_payable": ["Platform Coupon Payable", "Coupon Payable", "Accounts Payable"],
    "loyalty_payable":         ["Loyalty Payable", "Accounts Payable"],
    "vat_output":          ["Output VAT", "VAT", "Duties and Taxes"],
    "vat_input":           ["Input VAT", "VAT", "Duties and Taxes"],
    "accounts_receivable": ["Accounts Receivable", "Debtors"],
    "accounts_payable":    ["Accounts Payable", "Creditors"],
}

_VENDOR_FUZZY = {
    "cash_bank":              ["Cash/Bank", "Cash In Hand", "Cash"],
    "revenue":                ["Sales", "Product Revenue", "Revenue"],
    "vat_output":             ["Output VAT", "VAT", "Duties and Taxes"],
    "vat_input":              ["Input VAT", "VAT", "Duties and Taxes"],
    "clearing_platform":      ["SaathiMart Clearing", "Clearing - Platform", "Accounts Receivable"],
    "commission_expense":     ["Marketplace Commission", "Commission on Sales", "Indirect Expenses"],
    "platform_coupon_income": ["Platform Coupon Reimbursement", "Coupon Reimbursement", "Indirect Income"],
    "loyalty_income":         ["Loyalty Reimbursement", "Indirect Income"],
}

# Runtime cache so fuzzy lookups only hit the DB once per process lifetime.
_account_cache: dict = {}


def _get_account(account_key: str, entity: str = "platform") -> str | None:
    """Return the account name for account_key, falling back to fuzzy LIKE
    matching when the exact canonical name is not present in the chart.

    Strategy (same as vendor_accounting.py, now applied to the platform side):
      0. Return None immediately when ERPNext isn't installed (no tabAccount)
         — the hub can run standalone and GL recording is simply skipped.
      1. Return cached value immediately if we've resolved this key before.
      2. Try the exact canonical name from PLATFORM_ACCOUNTS / VENDOR_ACCOUNTS.
      3. Try each keyword in the fuzzy fallback list with a LIKE search,
         preferring non-group (leaf) accounts.
      4. Log and return None if nothing is found so callers can skip the
         entry rather than crashing.
    """
    if not frappe.db.exists("DocType", "Account"):
        return None

    cache_key = f"{entity}:{account_key}"
    if cache_key in _account_cache:
        return _account_cache[cache_key]

    accounts  = PLATFORM_ACCOUNTS if entity == "platform" else VENDOR_ACCOUNTS
    fuzzy_map = _PLATFORM_FUZZY   if entity == "platform" else _VENDOR_FUZZY

    # ── Step 1: exact match ───────────────────────────────────────────────
    canonical = accounts.get(account_key)
    if canonical and frappe.db.exists("Account", canonical):
        _account_cache[cache_key] = canonical
        return canonical

    # ── Step 2: LIKE search using fuzzy keywords ──────────────────────────
    for keyword in (fuzzy_map.get(account_key) or []):
        found = frappe.db.get_value(
            "Account",
            {"account_name": ["like", f"%{keyword}%"], "is_group": 0},
            "name",
        )
        if found:
            _account_cache[cache_key] = found
            return found

    frappe.log_error(
        f"Account '{account_key}' not found for entity '{entity}'. "
        "Run saathimart.api.setup_accounts.setup_platform_accounts to create it.",
        "Accounting",
    )
    return None


def _get_company():
    """Get the default company for this site.

    Returns None when ERPNext isn't installed (the hub can run standalone —
    GL entries simply aren't recorded). The global-default lookup itself is
    guarded: on a site without ERPNext there is no tabCompany table and the
    defaults query can 1146.
    """
    if not frappe.db.exists("DocType", "Company"):
        return None
    try:
        company = frappe.defaults.get_global_default("company")
        if not company:
            company = frappe.db.get_value("Company", {}, "name")
        return company
    except Exception:
        return None


def _get_default_cost_center(company=None):
    """Get the default cost center for marketplace operations.
    
    ERPNext requires cost_center for P&L accounts (Income/Expense).
    This function finds or creates a 'Marketplace' cost center.
    """
    if not company:
        company = _get_company()
    if not company:
        return None
    
    # Try to find existing Marketplace cost center
    cc = frappe.db.get_value(
        "Cost Center",
        {"company": company, "cost_center_name": "Marketplace", "is_group": 0},
        "name"
    )
    if cc:
        return cc
    
    # Fall back to any non-group cost center
    cc = frappe.db.get_value(
        "Cost Center",
        {"company": company, "is_group": 0},
        "name"
    )
    if cc:
        return cc
    
    # Last resort: get root and create Marketplace under it
    root = frappe.db.get_value(
        "Cost Center",
        {"company": company, "is_group": 1},
        "name"
    )
    if root:
        try:
            doc = frappe.new_doc("Cost Center")
            doc.cost_center_name = "Marketplace"
            doc.company = company
            doc.parent_cost_center = root
            doc.is_group = 0
            doc.insert(ignore_permissions=True, ignore_mandatory=True)
            return doc.name
        except Exception:
            pass
    
    return None


def _requires_cost_center(account):
    """Check if an account requires a cost center (P&L accounts)."""
    if not account:
        return False
    root_type = frappe.db.get_value("Account", account, "root_type")
    return root_type in ("Income", "Expense")


def create_gl_entry(account, debit=0, credit=0, voucher_type="Payment Entry",
                    voucher_no="", remarks="", party_type=None, party=None,
                    posting_date=None, cost_center=None):
    """Create a single GL Entry with proper cost center handling."""
    company = _get_company()
    if not company:
        frappe.log_error("No company found for GL Entry", "Accounting")
        return None

    # Verify account exists
    if not frappe.db.exists("Account", account):
        frappe.log_error(f"Account '{account}' does not exist", "Accounting")
        return None

    gl = frappe.new_doc("GL Entry")
    gl.posting_date = posting_date or nowdate()
    gl.account = account
    gl.debit = flt(debit, 2)
    gl.credit = flt(credit, 2)
    gl.voucher_type = voucher_type
    gl.voucher_no = voucher_no
    gl.remarks = remarks
    gl.company = company
    if party_type:
        gl.party_type = party_type
    if party:
        gl.party = party
    
    # Cost center is required for P&L accounts
    if _requires_cost_center(account):
        if not cost_center:
            cost_center = _get_default_cost_center(company)
        if cost_center:
            gl.cost_center = cost_center
    
    # ignore_links allows GL entries to be created before the voucher is fully persisted
    gl.insert(ignore_permissions=True, ignore_links=True)
    return gl


def create_gl_entries_batch(entries, voucher_type="Payment Entry", voucher_no="",
                           remarks="", posting_date=None):
    """Create multiple GL Entries in a batch. All entries must balance (sum debits = sum credits)."""
    company = _get_company()
    if not company:
        frappe.log_error("No company found for GL Entry batch", "Accounting")
        return []

    created = []
    for entry in entries:
        gl = create_gl_entry(
            account=entry["account"],
            debit=entry.get("debit", 0),
            credit=entry.get("credit", 0),
            voucher_type=voucher_type,
            voucher_no=voucher_no,
            remarks=remarks,
            party_type=entry.get("party_type"),
            party=entry.get("party"),
            posting_date=posting_date,
        )
        if gl:
            created.append(gl)
    return created


# ── Order Payment GL Entries ─────────────────────────────────────────────────
# Called after payment is confirmed (eSewa callback / COD delivery)

def record_order_payment_gl(order_id, amount, gateway="", reference=""):
    """
    Create GL Entries when customer pays for an order.

    This records:
      1. Cash/Bank debit (money received)
      2. Revenue credit (product sales)
      3. VAT Output credit (tax collected)
      4. Clearing Account debit (amount owed to vendors)
      5. Platform Coupon Expense debit (if platform absorbed coupon)
      6. Loyalty Expense debit (if loyalty points were redeemed)
      7. Delivery Income credit (if delivery charge exists)
    """
    order = frappe.get_doc("Order", order_id)
    if not order:
        return

    # Avoid double-entry
    if frappe.db.exists("GL Entry", {"voucher_no": order_id, "voucher_type": "Payment Entry"}):
        return

    entries = []
    company = _get_company()
    posting_date = nowdate()

    # ── Nepal marketplace model: the platform's PAN books only ITS OWN
    # income and liabilities. Product revenue and product Output VAT belong
    # to the VENDOR's PAN (they made the sale) — booking them here would
    # declare the vendors' sales on the platform's VAT return. What the
    # platform actually earns on this order:
    #   - marketplace commission (a service -> 13% service VAT)
    #   - nothing else (delivery is pass-through, coupons/loyalty are costs)
    # Everything else the customer paid sits in the vendor Clearing Account
    # until settlement.

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
    delivery_vat = rounded(delivery_charge * 13.0 / 113.0, 2)  # price-inclusive

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
    cash_account = _get_account("cash_bank")
    if cash_account:
        entries.append({
            "account": cash_account,
            "debit": flt(amount, 2),
            "credit": 0,
            "party_type": "Customer",
            "party": order.customer_email or order.customer_name,
        })

    # ── Step 2: Platform Coupon Expense (platform-funded discount) ──
    if platform_absorbs > 0:
        coupon_exp_account = _get_account("platform_coupon_exp")
        if coupon_exp_account:
            entries.append({
                "account": coupon_exp_account,
                "debit": platform_absorbs,
                "credit": 0,
                "remarks": f"Platform coupon absorbed for {order_id}",
            })

    # ── Step 3: Loyalty Expense (platform reimburses vendor) ──
    if loyalty > 0:
        loyalty_exp_account = _get_account("loyalty_expense")
        if loyalty_exp_account:
            entries.append({
                "account": loyalty_exp_account,
                "debit": loyalty,
                "credit": 0,
                "remarks": f"Loyalty points redeemed for {order_id}",
            })

    # ── Step 4: Marketplace Commission Income (platform's service fee) ──
    commission_account = _get_account("commission_income")
    if commission_account and commission_total > 0:
        entries.append({
            "account": commission_account,
            "debit": 0,
            "credit": commission_total,
            "remarks": f"Commission for {order_id} ({'; '.join(commission_detail)})",
        })

    # ── Step 5: Service VAT on commission (13% — platform's own VAT liability) ──
    vat_account = _get_account("vat_output")
    if vat_account and service_vat > 0:
        entries.append({
            "account": vat_account,
            "debit": 0,
            "credit": service_vat,
            "remarks": f"Service VAT on commission for {order_id}",
        })

    # ── Step 6: Delivery charge (pass-through service, price-inclusive VAT) ──
    if delivery_charge > 0:
        delivery_account = _get_account("delivery_income")
        if delivery_account:
            entries.append({
                "account": delivery_account,
                "debit": 0,
                "credit": flt(delivery_charge - delivery_vat, 2),
                "remarks": f"Delivery charge for {order_id}",
            })
            if delivery_vat > 0 and vat_account:
                entries.append({
                    "account": vat_account,
                    "debit": 0,
                    "credit": delivery_vat,
                    "remarks": f"Delivery charge VAT for {order_id}",
                })

    # ── Step 7: Vendor Clearing (residual = everything owed to vendors) ──
    # Balancing figure: cash-in + platform-funded discounts − platform's own
    # earnings. Settlement claims per-vendor amounts out of this account.
    clearing_amount = rounded(
        flt(amount) + platform_absorbs + loyalty
        - commission_total - service_vat - delivery_charge,
        2,
    )
    clearing_account = _get_account("clearing_vendor")
    if clearing_account and clearing_amount > 0:
        entries.append({
            "account": clearing_account,
            "debit": 0,
            "credit": clearing_amount,
            "remarks": f"Vendor clearing for {order_id}",
        })
    elif clearing_amount < 0:
        frappe.log_error(
            f"Negative vendor clearing {clearing_amount} for {order_id} — "
            "commission + VAT + delivery exceed the bill; check commission_pct",
            "Accounting",
        )

    # Create all entries
    if entries:
        create_gl_entries_batch(
            entries,
            voucher_type="Payment Entry",
            voucher_no=order_id,
            remarks=f"Payment received for order {order_id} via {gateway}",
            posting_date=posting_date,
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

    # Avoid double-entry
    if frappe.db.exists("GL Entry", {
        "voucher_no": order_id,
        "voucher_type": "Journal Entry",
        "remarks": ["like", "%Loyalty reimbursement%"]
    }):
        return

    loyalty_amount = flt(order.loyalty_discount)
    entries = []

    # Platform side: expense
    loyalty_exp_account = _get_account("loyalty_expense")
    clearing_account = _get_account("clearing_vendor")

    if loyalty_exp_account and clearing_account:
        # Platform debits loyalty expense, credits clearing account
        entries.append({
            "account": loyalty_exp_account,
            "debit": loyalty_amount,
            "credit": 0,
            "remarks": f"Loyalty reimbursement for {order_id}",
        })
        entries.append({
            "account": clearing_account,
            "debit": 0,
            "credit": loyalty_amount,
            "remarks": f"Loyalty reimbursement for {order_id}",
        })

        create_gl_entries_batch(
            entries,
            voucher_type="Journal Entry",
            voucher_no=order_id,
            remarks=f"Loyalty points reimbursement for order {order_id}",
        )


# ── Delivery Charge GL Entries ───────────────────────────────────────────────

def record_delivery_charge_gl(order_id):
    """
    Delivery charges flow through a separate logistics entity ledger.
    The vendor's PAN must NOT include delivery charges.
    
    If Saathimart/Courier handles delivery:
      - Courier issues VAT invoice to customer (or to Saathimart which passes through)
      - Treated as separate ledger flow
    """
    order = frappe.get_doc("Order", order_id)
    if not order or not flt(order.delivery_charge):
        return

    # Avoid double-entry
    if frappe.db.exists("GL Entry", {
        "voucher_no": order_id,
        "voucher_type": "Payment Entry",
        "remarks": ["like", "%Delivery charge%"]
    }):
        return

    delivery_amount = flt(order.delivery_charge)
    # The customer's delivery charge is VAT-INCLUSIVE (13% is inside what
    # they paid — same model as the ERPNext middleware, VAT Act s.12).
    # Back it out: VAT = amount × 13/113, income = amount × 100/113.
    delivery_vat = rounded(delivery_amount * 13.0 / 113.0, 2)

    entries = []

    # Clearing account for logistics
    logistics_clearing = _get_account("clearing_logistics")
    delivery_income = _get_account("delivery_income")
    vat_output = _get_account("vat_output")

    if logistics_clearing and delivery_income:
        # Debit clearing (logistics owes this), credit delivery income
        entries.append({
            "account": logistics_clearing,
            "debit": delivery_amount,
            "credit": 0,
            "remarks": f"Delivery charge for {order_id}",
        })
        entries.append({
            "account": delivery_income,
            "debit": 0,
            "credit": delivery_amount - delivery_vat,
            "remarks": f"Delivery service income for {order_id}",
        })
        if vat_output:
            entries.append({
                "account": vat_output,
                "debit": 0,
                "credit": delivery_vat,
                "remarks": f"Delivery VAT for {order_id}",
            })

        create_gl_entries_batch(
            entries,
            voucher_type="Payment Entry",
            voucher_no=order_id,
            remarks=f"Delivery charge accounting for order {order_id}",
        )


# ── Vendor Settlement Journal Entry ──────────────────────────────────────────

def _get_party_balance(account, party):
    """Current credit-positive balance of a liability account for one party
    (Supplier). Returns credits minus debits — what we still owe."""
    rows = frappe.db.sql(
        """
        SELECT SUM(credit - debit) AS bal
        FROM `tabGL Entry`
        WHERE account = %s AND party = %s AND is_cancelled = 0
        """,
        (account, party),
        as_dict=True,
    )
    return rounded(flt(rows[0].bal if rows else 0), 2)


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
    """
    entries = []
    posting_date = nowdate()
    tds = flt(tds_amount)

    # ── Debits: relieve the liabilities we owe the vendor ──
    # Promotional payables first (bounded by their actual balance so payouts
    # for ranges that straddle midnight still work), then clearing for the
    # remainder.
    relieved = flt(amount) + tds

    coupon_pay = _get_account("platform_coupon_payable")
    promo_coupon = rounded(min(flt(coupon_reimbursement),
                               _get_party_balance(coupon_pay, vendor_name)), 2) \
        if coupon_pay else 0.0
    if promo_coupon > 0:
        entries.append({
            "account": coupon_pay,
            "debit": promo_coupon,
            "credit": 0,
            "party_type": "Supplier",
            "party": vendor_name,
            "remarks": f"Platform coupon funding cleared for {vendor_name} — payout {payout_id}",
        })

    loyalty_pay = _get_account("loyalty_payable")
    promo_loyalty = rounded(min(flt(loyalty_reimbursement),
                                 _get_party_balance(loyalty_pay, vendor_name)), 2) \
        if loyalty_pay else 0.0
    if promo_loyalty > 0:
        entries.append({
            "account": loyalty_pay,
            "debit": promo_loyalty,
            "credit": 0,
            "party_type": "Supplier",
            "party": vendor_name,
            "remarks": f"Loyalty funding cleared for {vendor_name} — payout {payout_id}",
        })

    clearing_account = _get_account("clearing_vendor")
    if clearing_account:
        entries.append({
            "account": clearing_account,
            "debit": rounded(relieved - promo_coupon - promo_loyalty, 2),
            "credit": 0,
            "party_type": "Supplier",
            "party": vendor_name,
            "remarks": f"Clearing for {vendor_name} payout {payout_id}",
        })

    # ── Credits: cash leaves, withheld TDS sits in suspense ──
    bank_account = _get_account("cash_bank")
    if bank_account:
        entries.append({
            "account": bank_account,
            "debit": 0,
            "credit": flt(amount, 2),
            "party_type": "Supplier",
            "party": vendor_name,
            "remarks": f"Payout to {vendor_name} for {payout_id}",
        })

    if tds > 0:
        tds_account = _get_account("tds_payable")
        if tds_account:
            entries.append({
                "account": tds_account,
                "debit": 0,
                "credit": tds,
                "party_type": "Supplier",
                "party": vendor_name,
                "remarks": f"TDS withheld by {vendor_name} on commission (s88) — payout {payout_id}",
            })

    # Create journal entry
    if entries:
        create_gl_entries_batch(
            entries,
            voucher_type="Journal Entry",
            voucher_no=payout_id,
            remarks=f"Settlement for {vendor_name} ({payout_id})",
            posting_date=posting_date,
        )


# ── Credit Note for Loyalty Redemptions ──────────────────────────────────────

def create_loyalty_credit_note(order_id, vendor_name, loyalty_amount):
    """
    Auto-generate a Credit Note (Debit Note) from Vendor to Saathimart Platform
    for loyalty point redemptions.
    
    This tracks exactly how much SaathiMart Corporate owes that specific vendor
    for marketing promotions (loyalty points).
    """
    if flt(loyalty_amount) <= 0:
        return

    # Check if credit note already exists
    if frappe.db.exists("GL Entry", {
        "voucher_no": order_id,
        "voucher_type": "Journal Entry",
        "remarks": ["like", "%Loyalty credit note%"]
    }):
        return

    entries = []

    # Vendor side: receives reimbursement
    vendor_clearing = _get_account("clearing_platform", entity="vendor")
    loyalty_income = _get_account("loyalty_income", entity="vendor")

    if vendor_clearing and loyalty_income:
        entries.append({
            "account": vendor_clearing,
            "debit": flt(loyalty_amount, 2),
            "credit": 0,
            "remarks": f"Loyalty credit note for {order_id}",
        })
        entries.append({
            "account": loyalty_income,
            "debit": 0,
            "credit": flt(loyalty_amount, 2),
            "remarks": f"Loyalty credit note for {order_id}",
        })

        create_gl_entries_batch(
            entries,
            voucher_type="Journal Entry",
            voucher_no=order_id,
            remarks=f"Loyalty credit note from {vendor_name} for order {order_id}",
        )


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
    """Get all GL Entries for a specific order."""
    return frappe.get_all(
        "GL Entry",
        filters={"voucher_no": order_id},
        fields=["name", "posting_date", "account", "debit", "credit",
                "voucher_type", "voucher_no", "remarks", "party_type", "party"],
        order_by="creation asc",
    )


# ── Refund GL Entries ───────────────────────────────────────────────────────
def create_refund_gl_entries(order_id, refund_amount, reason=""):
    """
    Create reversal GL entries when a paid order is refunded.

    This reverses the original sale entries:
      DR: Product Revenue (reverses the sale)
      DR: Output VAT (reverses the tax)
      CR: Cash/Bank (money goes back to customer)

    The clearing account entries on the vendor side are NOT touched here —
    the vendor handles that when they receive the order.cancelled event.
    """
    if frappe.db.exists("GL Entry", {
        "voucher_no": order_id,
        "remarks": ["like", "%Refund%"],
    }):
        return  # already reversed

    doc = frappe.get_doc("Order", order_id)
    company = frappe.defaults.get_global_default("company") or frappe.db.get_value("Company", {}, "name")
    posting_date = frappe.utils.nowdate()

    entries = []

    # Cash/Bank credit (money returned)
    bank_account = frappe.db.get_value("Account", {"account_name": ["like", "%Cash%"], "is_group": 0}, "name")
    if bank_account:
        entries.append({
            "account": bank_account,
            "debit": 0,
            "credit": flt(refund_amount, 2),
            "remarks": f"Refund for {order_id}: {reason}",
        })

    # Product Revenue debit (reverses the sale)
    revenue_account = frappe.db.get_value("Account", {"account_name": ["like", "%Sales%"], "is_group": 0}, "name")
    if revenue_account:
        entries.append({
            "account": revenue_account,
            "debit": flt(refund_amount, 2),
            "credit": 0,
            "remarks": f"Refund reversal for {order_id}",
        })

    for entry in entries:
        gl = frappe.new_doc("GL Entry")
        gl.posting_date = posting_date
        gl.account = entry["account"]
        gl.debit = entry["debit"]
        gl.credit = entry["credit"]
        gl.voucher_type = "Journal Entry"
        gl.voucher_no = order_id
        gl.remarks = entry["remarks"]
        gl.company = company
        gl.insert(ignore_permissions=True, ignore_links=True)

