# Copyright (c) 2026, DnDTS Tech Nepal and contributors
# For license information, please see license.txt

"""Vendor Settlement Aging — money owed to vendors, by how long it's owed.

The platform collects the customer's cash and owes each vendor the clearing
balance. Vendor Payout is that promise. This report answers the two
questions an ops team actually asks:

  1. Who is owed what, and for how long? (aging buckets on unpaid payouts)
  2. Is the payout pipeline healthy? (throughput: approved vs paid vs
     cancelled, totals paid this period)

Rows are unpaid payouts (Draft/Approved) bucketed by days since period_end —
the date the vendor finished earning the money, not the date we noticed it.
Paid payouts appear only in the health summary, so the working list stays
short.
"""
import frappe
from frappe import _
from frappe.utils import getdate, now_datetime


def execute(filters=None):
    columns = [
        {"label": _("Payout"), "fieldname": "name", "fieldtype": "Link", "options": "Vendor Payout", "width": 160},
        {"label": _("Vendor"), "fieldname": "vendor_name", "fieldtype": "Data", "width": 180},
        {"label": _("Status"), "fieldname": "status", "fieldtype": "Data", "width": 100},
        {"label": _("Period End"), "fieldname": "period_end", "fieldtype": "Date", "width": 110},
        {"label": _("Sales"), "fieldname": "total_sales", "fieldtype": "Currency", "width": 120},
        {"label": _("Commission"), "fieldname": "commission_amount", "fieldtype": "Currency", "width": 120},
        {"label": _("Payout Due"), "fieldname": "payout_amount", "fieldtype": "Currency", "width": 130},
        {"label": _("Days Owed"), "fieldname": "days_owed", "fieldtype": "Int", "width": 100},
        {"label": _("Aging Bucket"), "fieldname": "aging_bucket", "fieldtype": "Data", "width": 110},
    ]

    rows = frappe.get_all(
        "Vendor Payout",
        filters={"status": ("in", ["Draft", "Approved"])},
        fields=[
            "name", "vendor_name", "status", "period_end", "total_sales",
            "commission_amount", "payout_amount",
        ],
        order_by="period_end asc",
        limit=500,
    )

    now = now_datetime()
    data = []
    bucket_totals = {}
    for r in rows:
        days = (getdate(now) - getdate(r.period_end)).days if r.period_end else 0
        bucket = _bucket(days)
        bucket_totals.setdefault(bucket, {"count": 0, "due": 0.0})
        bucket_totals[bucket]["count"] += 1
        bucket_totals[bucket]["due"] += r.payout_amount or 0
        data.append({
            **r,
            "days_owed": days,
            "aging_bucket": bucket,
        })

    for bucket in ("Current", "1-7d", "8-30d", "30d+"):
        if bucket in bucket_totals:
            b = bucket_totals[bucket]
            data.append(frappe._dict({
                "vendor_name": f"— {bucket}: {b['count']} payouts, due रु{b['due']:.2f} —",
                "payout_amount": b["due"],
            }))

    # Pipeline health: this month's throughput by status.
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    health_rows = frappe.get_all(
        "Vendor Payout",
        filters={"creation": (">=", month_start)},
        fields=["status", "payout_amount"],
    )
    health = {}
    for h in health_rows:
        agg = health.setdefault(h.status, [0, 0.0])
        agg[0] += 1
        agg[1] += h.payout_amount or 0
    summary = ", ".join(
        f"{s}: {n} (रु{amt:.0f})" for s, (n, amt) in sorted(health.items())
    )
    if summary:
        data.append(frappe._dict({"vendor_name": f"— This month: {summary} —"}))

    return columns, data


def _bucket(days):
    if days <= 0:
        return "Current"
    if days <= 7:
        return "1-7d"
    if days <= 30:
        return "8-30d"
    return "30d+"
