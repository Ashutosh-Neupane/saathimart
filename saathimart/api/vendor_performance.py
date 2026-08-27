"""
Vendor performance metrics — automated scorecard calculation based on
delivery rate, acceptance speed, stock accuracy, and customer ratings.
"""
import frappe
from frappe import _
from frappe.utils import flt, now_datetime, add_to_date, time_diff_in_seconds


@frappe.whitelelist()
def get_vendor_scorecard(vendor_name, days=30):
    """Calculate performance scorecard for a vendor over the last N days."""
    if not vendor_name:
        frappe.throw(_("vendor is required"))

    cutoff = add_to_date(now_datetime(), days=-days)

    # Total orders in period
    total_orders = frappe.db.count("Vendor Fulfillment", {
        "vendor": vendor_name,
        "creation": (">=", cutoff),
    })

    # Delivered orders
    delivered = frappe.db.count("Vendor Fulfillment", {
        "vendor": vendor_name,
        "status": "Delivered",
        "creation": (">=", cutoff),
    })

    # Cancelled orders
    cancelled = frappe.db.count("Vendor Fulfillment", {
        "vendor": vendor_name,
        "status": "Cancelled",
        "creation": (">=", cutoff),
    })

    # Acceptance speed (time from Pending to Confirmed)
    acceptance_times = frappe.db.sql("""
        SELECT TIMESTAMPDIFF(SECOND, creation, modified) as seconds
        FROM `tabVendor Fulfillment`
        WHERE vendor = %s AND status != 'Pending' AND creation >= %s
    """, (vendor_name, cutoff), as_dict=True)
    avg_acceptance_seconds = (
        sum(t.seconds for t in acceptance_times) / len(acceptance_times)
        if acceptance_times else 0
    )

    # Stock accuracy (from reconciliation logs)
    stock_issues = frappe.db.count("Error Log", {
        "method": ("like", "%Stock Reconciliation%"),
        "creation": (">=", cutoff),
    })

    # Customer ratings
    ratings = frappe.db.sql("""
        SELECT AVG(r.rating) as avg_rating, COUNT(r.name) as review_count
        FROM `tabProduct Review` r
        INNER JOIN `tabVendor Fulfillment` vf ON r.order_id = vf.parent
        WHERE vf.vendor = %s AND r.creation >= %s
    """, (vendor_name, cutoff), as_dict=True)
    avg_rating = flt(ratings[0].avg_rating) if ratings else 0
    review_count = ratings[0].review_count if ratings else 0

    # Calculate scores
    delivery_rate = (delivered / total_orders * 100) if total_orders > 0 else 0
    cancellation_rate = (cancelled / total_orders * 100) if total_orders > 0 else 0
    acceptance_speed_minutes = avg_acceptance_seconds / 60

    # Overall score (weighted average)
    overall_score = (
        delivery_rate * 0.4 +              # 40% weight
        min(100, 100 - cancellation_rate) * 0.2 +  # 20% weight
        min(100, max(0, 100 - stock_issues * 10)) * 0.2 +  # 20% weight
        (avg_rating / 5 * 100) * 0.2       # 20% weight
    )

    return {
        "vendor": vendor_name,
        "period_days": days,
        "total_orders": total_orders,
        "delivered": delivered,
        "cancelled": cancelled,
        "delivery_rate": round(delivery_rate, 1),
        "cancellation_rate": round(cancellation_rate, 1),
        "avg_acceptance_minutes": round(acceptance_speed_minutes, 1),
        "stock_issues": stock_issues,
        "avg_rating": round(avg_rating, 2),
        "review_count": review_count,
        "overall_score": round(overall_score, 1),
    }


@frappe.whitelelist()
def get_all_vendor_scores(days=30):
    """Get performance scores for all active vendors."""
    vendors = frappe.get_all(
        "Vendor",
        filters={"status": "Active"},
        fields=["name", "vendor_name"],
    )

    results = []
    for v in vendors:
        try:
            score = get_vendor_scorecard(v.name, days)
            score["vendor_name"] = v.vendor_name
            results.append(score)
        except Exception:
            continue

    # Sort by overall score descending
    results.sort(key=lambda x: x.get("overall_score", 0), reverse=True)
    return results
