"""
Loyalty points engine — earn, redeem, balance, tier resolution.
No ERPNext dependency. All data lives in Loyalty Point Entry.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, add_days, today, nowdate


# ── Balance ───────────────────────────────────────────────────────────────────

def get_balance(customer_email: str) -> float:
    """Return current redeemable point balance for a customer."""
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
        FROM `tabLoyalty Point Entry`
        WHERE customer_email = %s AND is_expired = 0
    """, customer_email)
    return flt(result[0][0]) if result else 0.0


def get_tier(customer_email: str, program_name: str) -> dict:
    """Return the customer's current tier and next tier info."""
    balance = get_balance(customer_email)
    program = frappe.get_doc("Loyalty Program", program_name)
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


# ── Earn ──────────────────────────────────────────────────────────────────────

def _get_zone_loyalty_multiplier(order_name: str) -> float:
    """
    Location-based loyalty rate: the multiplier lives on the order's
    Delivery Zone (Delivery Zone.loyalty_multiplier), not on the customer —
    the same customer earns at a different rate depending on which zone the
    order was delivered to. Defaults to 1.0 (no change) when the order has
    no zone or the zone has no override.
    """
    if not order_name:
        return 1.0
    zone = frappe.db.get_value("Order", order_name, "delivery_zone")
    if not zone:
        return 1.0
    multiplier = frappe.db.get_value("Delivery Zone", zone, "loyalty_multiplier")
    return flt(multiplier) if multiplier is not None else 1.0


def earn_points(customer_email: str, order_name: str, order_amount: float) -> float:
    """
    Calculate and record points earned for a delivered/paid order.
    Returns points earned.
    """
    s = frappe.get_single("Settings")
    if not s.enable_loyalty or not s.loyalty_program:
        return 0.0

    program = frappe.get_doc("Loyalty Program", s.loyalty_program)
    if not program.is_active:
        return 0.0

    # Apply tier multiplier and the order's zone-based loyalty multiplier
    tier_info = get_tier(customer_email, program.name)
    tier_multiplier = tier_info["current_tier"]["multiplier"]
    zone_multiplier = _get_zone_loyalty_multiplier(order_name)
    base_points = flt(order_amount) * flt(program.collection_factor) * tier_multiplier * zone_multiplier
    points = round(base_points, 2)

    if points <= 0:
        return 0.0

    expiry = None
    if program.point_expiry_days:
        expiry = add_days(today(), program.point_expiry_days)

    entry = frappe.new_doc("Loyalty Point Entry")
    entry.customer_email = customer_email
    entry.program = program.name
    entry.points = points
    entry.entry_type = "Earned"
    if frappe.db.exists("Order", order_name):
        entry.order = order_name
    entry.expiry_date = expiry
    entry.insert(ignore_permissions=True)
    frappe.db.commit()

    return points


# ── Redeem ────────────────────────────────────────────────────────────────────

def calculate_redemption_discount(customer_email: str, points_to_redeem: float,
                                   order_subtotal: float) -> dict:
    """
    Validate and calculate the discount for redeeming points.
    Returns {"ok": bool, "discount": float, "points_used": float, "error": str}
    """
    s = frappe.get_single("Settings")
    if not s.enable_loyalty or not s.loyalty_program:
        # Canonical error shape (see api/responses.py); discount/points_used
        # zeros stay so callers can keep doing arithmetic on the result.
        return {"ok": False, "error": "Loyalty not enabled", "error_code": "VALIDATION_ERROR",
                "discount": 0, "points_used": 0}

    program = frappe.get_doc("Loyalty Program", s.loyalty_program)
    points_to_redeem = flt(points_to_redeem)

    if points_to_redeem < program.min_points_to_redeem:
        return {
            "ok": False, "error": f"Minimum {program.min_points_to_redeem} points required to redeem",
            "error_code": "VALIDATION_ERROR",
            "discount": 0, "points_used": 0,
        }

    balance = get_balance(customer_email)
    if points_to_redeem > balance:
        points_to_redeem = balance  # cap to available balance

    # Cap by max redemption % of order
    max_discount = flt(order_subtotal) * (flt(program.max_redemption_per_order_pct) / 100)
    discount = min(points_to_redeem * flt(program.redemption_factor), max_discount)
    actual_points = discount / flt(program.redemption_factor) if program.redemption_factor else 0

    return {
        "ok": True,
        "discount": round(discount, 2),
        "points_used": round(actual_points, 2),
    }


def redeem_points(customer_email: str, order_name: str, points: float, discount: float):
    """Record a redemption entry after order is placed."""
    s = frappe.get_single("Settings")
    if not s.loyalty_program:
        return

    entry = frappe.new_doc("Loyalty Point Entry")
    entry.customer_email = customer_email
    entry.program = s.loyalty_program
    entry.points = points
    entry.entry_type = "Redeemed"
    entry.order = order_name
    entry.remarks = f"Redeemed for NPR {discount} discount"
    entry.insert(ignore_permissions=True)
    frappe.db.commit()


# ── Expiry cron ───────────────────────────────────────────────────────────────

def expire_old_points():
    """Daily cron — mark expired point entries."""
    frappe.db.sql("""
        UPDATE `tabLoyalty Point Entry`
        SET is_expired = 1, entry_type = 'Expired'
        WHERE is_expired = 0
          AND expiry_date IS NOT NULL
          AND expiry_date < %s
          AND entry_type = 'Earned'
    """, nowdate())
    frappe.db.commit()


# ── Public API ────────────────────────────────────────────────────────────────

@frappe.whitelist()
def get_loyalty_balance(customer_email=None):
    email = customer_email or frappe.session.user
    s = frappe.get_single("Settings")
    if not s.enable_loyalty or not s.loyalty_program:
        return {"enabled": False, "balance": 0}

    tier_info = get_tier(email, s.loyalty_program)
    return {"enabled": True, **tier_info}


@frappe.whitelist(allow_guest=True)
def preview_redemption(points_to_redeem, order_subtotal):
    email = frappe.session.user
    if email == "Guest":
        return {"ok": False, "error": "Login required to redeem points",
                "error_code": "UNAUTHORIZED"}
    return calculate_redemption_discount(email, flt(points_to_redeem), flt(order_subtotal))
