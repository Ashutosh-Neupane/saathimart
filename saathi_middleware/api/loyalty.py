"""
Loyalty points engine — earn, redeem, balance, tier resolution.
All data lives in SM Loyalty Point Entry.
"""
from __future__ import annotations

import math

import frappe
from frappe import _
from frappe.utils import flt, add_days, today, nowdate


def get_balance(customer_email: str) -> float:
    result = frappe.db.sql("""
        SELECT COALESCE(SUM(
            CASE entry_type
                WHEN 'Earned'   THEN points
                WHEN 'Adjusted' THEN points
                WHEN 'Redeemed' THEN -points
                WHEN 'Expired'  THEN -points
                ELSE 0
            END
        ), 0)
        FROM `tabSM Loyalty Point Entry`
        WHERE customer_email = %s AND is_expired = 0
    """, customer_email)
    return flt(result[0][0]) if result else 0.0


def get_tier(customer_email: str, program_name: str) -> dict:
    balance = get_balance(customer_email)
    program = frappe.get_doc("SM Loyalty Program", program_name)
    tiers = sorted(program.tiers or [], key=lambda t: t.min_points)

    current = {"tier_name": "Base", "min_points": 0, "multiplier": 1.0, "badge_color": "#6b7280"}
    next_tier = None

    for tier in tiers:
        if balance >= tier.min_points:
            current = {
                "tier_name": tier.tier_name,
                "min_points": tier.min_points,
                "multiplier": flt(tier.multiplier),
                "badge_color": tier.badge_color or "#16a34a",
            }
        else:
            if next_tier is None:
                next_tier = {
                    "tier_name": tier.tier_name,
                    "min_points": tier.min_points,
                    "points_needed": tier.min_points - balance,
                }
            break

    return {
        "balance": balance,
        "current_tier": current,
        "next_tier": next_tier,
    }


def earn_points(customer_email: str, order_name: str, order_amount: float) -> float:
    s = frappe.get_single("Saathi Settings")
    if not getattr(s, "enable_loyalty", 0) or not getattr(s, "loyalty_program", None):
        return 0.0

    program = frappe.get_doc("SM Loyalty Program", s.loyalty_program)
    if not program.is_active:
        return 0.0

    tier_info = get_tier(customer_email, program.name)
    multiplier = tier_info["current_tier"]["multiplier"]
    # Points are a whole-number currency everywhere else in this system (the
    # storefront rejects a fractional redemption outright, min_points_to_redeem
    # is an Int) — rounding to 2 decimal places here was the actual bug: a
    # NPR 4,250 order at the default 1%-collection rate earns exactly 42.5,
    # which then can never be redeemed. Floor instead, so a customer is never
    # credited a fraction they can't spend.
    base_points = flt(order_amount) * flt(program.collection_factor) * multiplier
    points = math.floor(base_points)

    if points <= 0:
        return 0.0

    expiry = None
    if program.point_expiry_days:
        expiry = add_days(today(), program.point_expiry_days)

    entry = frappe.new_doc("SM Loyalty Point Entry")
    entry.customer_email = customer_email
    entry.program = program.name
    entry.points = points
    entry.entry_type = "Earned"
    if frappe.db.exists("Saathi Order", order_name):
        entry.order = order_name
    entry.expiry_date = expiry
    entry.insert(ignore_permissions=True)
    frappe.db.commit()

    return points


def calculate_redemption_discount(customer_email: str, points_to_redeem: float,
                                   order_subtotal: float) -> dict:
    s = frappe.get_single("Saathi Settings")
    if not getattr(s, "enable_loyalty", 0) or not getattr(s, "loyalty_program", None):
        return {"ok": False, "discount": 0, "points_used": 0, "error": "Loyalty not enabled"}

    program = frappe.get_doc("SM Loyalty Program", s.loyalty_program)
    # Floor here too, not just at earn time — a balance can still be
    # fractional from before this was fixed (or from an SM Admin manually
    # adjusting it), and a caller could still pass a fractional value
    # directly. Flooring the balance means a 42.5-point balance redeems as
    # 42 instead of refusing to redeem at all; the leftover 0.5 simply isn't
    # spendable, same as real currency has no sub-cent coins.
    points_to_redeem = math.floor(flt(points_to_redeem))

    if points_to_redeem < program.min_points_to_redeem:
        return {
            "ok": False, "discount": 0, "points_used": 0,
            "error": f"Minimum {program.min_points_to_redeem} points required to redeem",
        }

    balance = math.floor(get_balance(customer_email))
    if points_to_redeem > balance:
        points_to_redeem = balance

    max_discount = flt(order_subtotal) * (flt(program.max_redemption_per_order_pct) / 100)
    discount = points_to_redeem * flt(program.redemption_factor)
    if program.redemption_factor and discount > max_discount:
        # Re-floor after capping to the per-order discount ceiling, so
        # points_used stays a whole number even when the cap — not the
        # balance — is what limits how many points actually get spent.
        points_to_redeem = math.floor(max_discount / flt(program.redemption_factor))
        discount = points_to_redeem * flt(program.redemption_factor)

    return {
        "ok": True,
        "discount": round(discount, 2),
        "points_used": points_to_redeem,
    }


def redeem_points(customer_email: str, order_name: str, points: float, discount: float):
    s = frappe.get_single("Saathi Settings")
    if not getattr(s, "loyalty_program", None):
        return

    entry = frappe.new_doc("SM Loyalty Point Entry")
    entry.customer_email = customer_email
    entry.program = s.loyalty_program
    entry.points = points
    entry.entry_type = "Redeemed"
    entry.order = order_name
    entry.remarks = f"Redeemed for NPR {discount} discount"
    entry.insert(ignore_permissions=True)
    frappe.db.commit()


def expire_old_points():
    frappe.db.sql("""
        UPDATE `tabSM Loyalty Point Entry`
        SET is_expired = 1, entry_type = 'Expired'
        WHERE is_expired = 0
          AND expiry_date IS NOT NULL
          AND expiry_date < %s
          AND entry_type = 'Earned'
    """, nowdate())
    frappe.db.commit()


@frappe.whitelist()
def get_loyalty_balance(customer_email=None):
    # Only staff may look up another customer's balance — otherwise any
    # logged-in customer could read anyone else's loyalty balance by passing
    # their email, since this is a plain function call with no per-doc
    # permission check (there's no "doc" here for has_permission to gate).
    if customer_email and customer_email != frappe.session.user:
        if not {"SM Admin", "SM Vendor"} & set(frappe.get_roles()):
            frappe.throw(_("Not permitted to view this customer's loyalty balance"), frappe.PermissionError)
        email = customer_email
    else:
        email = frappe.session.user
    if email == "Guest":
        frappe.throw(_("Not logged in"), frappe.PermissionError)

    s = frappe.get_single("Saathi Settings")
    if not getattr(s, "enable_loyalty", 0) or not getattr(s, "loyalty_program", None):
        return {"enabled": False, "balance": 0}

    tier_info = get_tier(email, s.loyalty_program)
    return {"enabled": True, **tier_info}


@frappe.whitelist(allow_guest=True)
def preview_redemption(points_to_redeem, order_subtotal):
    email = frappe.session.user
    if email == "Guest":
        return {"ok": False, "error": "Login required to redeem points"}
    return calculate_redemption_discount(email, flt(points_to_redeem), flt(order_subtotal))
