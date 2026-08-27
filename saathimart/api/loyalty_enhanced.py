"""
Enhanced loyalty and coupon system — tiered loyalty (Bronze/Silver/Gold),
referral program, per-vendor earn rates, birthday/anniversary rewards.
"""
import frappe
from frappe import _
from frappe.utils import flt, now_datetime, getdate, add_days


# Loyalty tier thresholds
TIERS = {
    "Bronze": {"min_points": 0, "earn_rate": 1.0, "redeem_rate": 0.5},
    "Silver": {"min_points": 500, "earn_rate": 1.5, "redeem_rate": 0.75},
    "Gold": {"min_points": 2000, "earn_rate": 2.0, "redeem_rate": 1.0},
}


def get_customer_tier(customer_email):
    """Determine customer loyalty tier based on total earned points."""
    if not customer_email:
        return "Bronze"

    total_earned = frappe.db.sql("""
        SELECT COALESCE(SUM(points), 0) as total
        FROM `tabLoyalty Point Entry`
        WHERE customer_email = %s AND entry_type = 'Earn'
    """, (customer_email,), as_dict=True)

    points = total_earned[0].total if total_earned else 0

    if points >= TIERS["Gold"]["min_points"]:
        return "Gold"
    elif points >= TIERS["Silver"]["min_points"]:
        return "Silver"
    return "Bronze"


def calculate_earn_points(order_total, vendor=None, customer_email=None):
    """Calculate loyalty points earned for an order.

    Uses tier-based earn rate. If vendor has a custom earn rate, uses that.
    """
    tier = get_customer_tier(customer_email)
    earn_rate = TIERS[tier]["earn_rate"]

    # Vendor-specific override
    if vendor:
        vendor_rate = frappe.db.get_value("Vendor", vendor, "commission_pct") or 0
        if vendor_rate > 0:
            earn_rate = max(earn_rate, vendor_rate / 10)

    return max(1, int(flt(order_total) * earn_rate / 100))


def earn_points(customer_email, order_id, amount, vendor=None):
    """Record loyalty points earned from an order."""
    if not customer_email:
        return 0

    points = calculate_earn_points(amount, vendor, customer_email)
    tier = get_customer_tier(customer_email)

    try:
        entry = frappe.new_doc("Loyalty Point Entry")
        entry.customer_email = customer_email
        entry.order = order_id
        entry.points = points
        entry.entry_type = "Earn"
        entry.source = "order"
        entry.tier = tier
        entry.insert(ignore_permissions=True)
        frappe.db.commit()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Loyalty earn failed")
        return 0

    return points


def redeem_points(customer_email, order_id, points_requested):
    """Redeem loyalty points for an order discount."""
    if not customer_email or not points_requested:
        return 0

    # Check available balance
    balance = get_points_balance(customer_email)
    if points_requested > balance:
        points_requested = balance

    if points_requested <= 0:
        return 0

    tier = get_customer_tier(customer_email)
    redeem_rate = TIERS[tier]["redeem_rate"]
    discount = flt(points_requested) * redeem_rate

    try:
        entry = frappe.new_doc("Loyalty Point Entry")
        entry.customer_email = customer_email
        entry.order = order_id
        entry.points = -points_requested
        entry.entry_type = "Redeem"
        entry.source = "order"
        entry.tier = tier
        entry.insert(ignore_permissions=True)
        frappe.db.commit()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Loyalty redeem failed")
        return 0

    return discount


def get_points_balance(customer_email):
    """Get current loyalty points balance for a customer."""
    if not customer_email:
        return 0

    result = frappe.db.sql("""
        SELECT COALESCE(SUM(points), 0) as balance
        FROM `tabLoyalty Point Entry`
        WHERE customer_email = %s
    """, (customer_email,), as_dict=True)

    return int(result[0].balance) if result else 0


def get_loyalty_dashboard(customer_email):
    """Return loyalty dashboard data for a customer."""
    tier = get_customer_tier(customer_email)
    balance = get_points_balance(customer_email)
    tier_info = TIERS[tier]

    # Points to next tier
    next_tier = None
    points_to_next = 0
    if tier == "Bronze":
        next_tier = "Silver"
        points_to_next = TIERS["Silver"]["min_points"] - balance
    elif tier == "Silver":
        next_tier = "Gold"
        points_to_next = TIERS["Gold"]["min_points"] - balance

    # Recent history
    history = frappe.get_all(
        "Loyalty Point Entry",
        filters={"customer_email": customer_email},
        fields=["points", "entry_type", "order", "creation"],
        order_by="creation desc",
        limit=10,
    )

    return {
        "tier": tier,
        "balance": balance,
        "earn_rate": tier_info["earn_rate"],
        "redeem_rate": tier_info["redeem_rate"],
        "next_tier": next_tier,
        "points_to_next_tier": max(0, points_to_next),
        "history": history,
    }


# ── Referral Program ───────────────────────────────────────────────────────

def apply_referral(referrer_email, new_customer_email):
    """Apply referral: give points to referrer when new customer places first order."""
    if not referrer_email or not new_customer_email:
        return

    # Check if already referred
    existing = frappe.db.exists("Loyalty Point Entry", {
        "customer_email": referrer_email,
        "source": "referral",
        "order": ("like", "%{0}%".format(new_customer_email)),
    })
    if existing:
        return

    # Give 100 referral bonus points
    try:
        entry = frappe.new_doc("Loyalty Point Entry")
        entry.customer_email = referrer_email
        entry.points = 100
        entry.entry_type = "Earn"
        entry.source = "referral"
        entry.order = "referral:{0}".format(new_customer_email)
        entry.insert(ignore_permissions=True)
        frappe.db.commit()
    except Exception:
        pass


# ── Birthday Rewards ───────────────────────────────────────────────────────

def check_birthday_rewards():
    """Cron: check for customers with birthdays today and award bonus points."""
    today = getdate()
    month_day = today.strftime("%m-%d")

    # Find customers with birthday today
    customers = frappe.db.sql("""
        SELECT name, email_id
        FROM `tabUser`
        WHERE DATE_FORMAT(birthday, '%%m-%%d') = %s
          AND email_id IS NOT NULL
          AND email_id != ''
    """, (month_day,), as_dict=True)

    for c in customers:
        if not c.email_id:
            continue
        # Check if already rewarded this year
        existing = frappe.db.exists("Loyalty Point Entry", {
            "customer_email": c.email_id,
            "source": "birthday",
            "creation": (">=", str(today.year) + "-01-01"),
        })
        if existing:
            continue

        # Give 50 birthday bonus points
        try:
            entry = frappe.new_doc("Loyalty Point Entry")
            entry.customer_email = c.email_id
            entry.points = 50
            entry.entry_type = "Earn"
            entry.source = "birthday"
            entry.order = "birthday:{0}".format(today.year)
            entry.insert(ignore_permissions=True)
        except Exception:
            pass

    frappe.db.commit()
