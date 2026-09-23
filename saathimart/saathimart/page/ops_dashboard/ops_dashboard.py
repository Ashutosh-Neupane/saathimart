"""SaathiMart Ops Dashboard — server-computed snapshot for /app/ops-dashboard.

One whitelisted call returns every number the page shows. All queries are
count/sum aggregations on indexed columns (status/creation) — no doc loads —
so the page stays cheap enough to auto-refresh during business hours.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, now_datetime, today


def _day_bounds():
    day = today()
    return day, day + " 23:59:59"


def _count(dt, filters):
    return cint(frappe.db.count(dt, filters))


def _sum(dt, field, filters):
    rows = frappe.get_all(dt, filters=filters, fields=[field], as_list=True)
    return float(sum(r[0] or 0 for r in rows))


@frappe.whitelist()
def get_snapshot():
    day, day_end = _day_bounds()
    now = now_datetime()

    # ── Today ────────────────────────────────────────────────────────────
    orders_today = _count("Order", {"creation": ("between", [day, day_end])})
    gmv_today = _sum("Order", "grand_total", {"creation": ("between", [day, day_end])})
    paid_today = _count(
        "Order", {"creation": ("between", [day, day_end]), "payment_status": "Paid"}
    )
    cancelled_today = _count(
        "Order", {"creation": ("between", [day, day_end]), "status": "Cancelled"}
    )
    aov = round(gmv_today / orders_today, 2) if orders_today else 0

    today_cards = [
        {"label": _("Orders"), "value": orders_today, "sub": f"Paid: {paid_today}"},
        {"label": _("GMV"), "value": f"रु{gmv_today:,.2f}", "sub": f"AOV: रु{aov:,.0f}"},
        {
            "label": _("Cancellations"),
            "value": cancelled_today,
            "warn": cancelled_today > max(3, orders_today * 0.2) if orders_today else False,
            "sub": f"{round(100 * cancelled_today / orders_today, 1)}%" if orders_today else "",
        },
    ]

    # ── Pipeline health ──────────────────────────────────────────────────
    pending_orders = _count("Order", {"status": "Pending"})
    dead_letters = _count("Webhook Event", {"status": "Dead"})
    queued = _count("Webhook Event", {"status": "Queued"})
    failed_24h = _count(
        "Webhook Event",
        {"status": "Failed", "creation": (">=", day + " 00:00:00")},
    )
    pipeline_cards = [
        {"label": _("Orders Awaiting Confirm"), "value": pending_orders},
        {"label": _("Events Queued"), "value": queued},
        {"label": _("Events Failed (24h)"), "value": failed_24h},
        {
            "label": _("Dead Letters"),
            "value": dead_letters,
            "warn": dead_letters > 0,
            "sub": _("needs manual replay") if dead_letters else _("clean"),
        },
    ]

    # ── Money ────────────────────────────────────────────────────────────
    unpaid_orders = _count(
        "Order",
        {"payment_status": ("in", ["Unpaid", "Partially Paid"]),
         "status": ("not in", ["Cancelled", "Refunded"])},
    )
    unpaid_value = _sum(
        "Order", "grand_total",
        {"payment_status": ("in", ["Unpaid", "Partially Paid"]),
         "status": ("not in", ["Cancelled", "Refunded"])},
    )
    payouts_due = _sum(
        "Vendor Payout", "payout_amount", {"status": ("in", ["Draft", "Approved"])}
    )
    payouts_due_n = _count("Vendor Payout", {"status": ("in", ["Draft", "Approved"])})
    money_cards = [
        {"label": _("Unpaid Orders"), "value": unpaid_orders,
         "sub": f"रु{unpaid_value:,.2f} stuck"},
        {"label": _("Payouts Due"), "value": f"रु{payouts_due:,.2f}",
         "sub": f"{payouts_due_n} payouts pending"},
    ]

    # ── Alerts ───────────────────────────────────────────────────────────
    alerts = []
    if dead_letters:
        alerts.append({
            "level": "danger",
            "text": _("{0} dead-letter events need replay — check Webhook Event list").format(dead_letters),
        })
    if queued > 50:
        alerts.append({
            "level": "warning",
            "text": _("{0} events queued — delivery workers may be down").format(queued),
        })
    hour = now.hour
    if 9 <= hour <= 21 and unpaid_orders > 10:
        alerts.append({
            "level": "warning",
            "text": _("{0} orders unpaid (रु{1}) — consider recovery nudge").format(unpaid_orders, f"{unpaid_value:,.0f}"),
        })
    if payouts_due > 0:
        alerts.append({
            "level": "warning",
            "text": _("रु{0} owed to {1} vendors — run Vendor Settlement Aging").format(
                f"{payouts_due:,.0f}", payouts_due_n
            ),
        })

    return {
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "today": today_cards,
        "pipeline": pipeline_cards,
        "money": money_cards,
        "alerts": alerts,
    }
