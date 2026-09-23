# Copyright (c) 2026, DnDTS Tech Nepal and contributors
# For license information, please see license.txt

"""Abandoned Carts — revenue stuck before payment, plus dead guest carts.

Two sections in one report:
  1. Orders that reached checkout but never got paid: ``payment_status`` is
     Unpaid/Partially Paid and the order is not cancelled — money we showed
     the customer but never collected. Age-bucketed so ops can see what is
     fresh (recoverable, e.g. nudge email) versus old (likely lost).
  2. Carts still Active but expired per their own ``expires_at`` — shoppers
     who never even reached checkout. Counts + value, no order rows.
"""
import frappe
from frappe import _
from frappe.utils import cint, now_datetime


def execute(filters=None):
    filters = filters or {}
    hours = cint(filters.get("min_age_hours") or 24)

    now = now_datetime()
    columns = [
        {"label": _("Order ID"), "fieldname": "name", "fieldtype": "Link", "options": "Order", "width": 160},
        {"label": _("Customer"), "fieldname": "customer_name", "fieldtype": "Data", "width": 160},
        {"label": _("Phone"), "fieldname": "customer_phone", "fieldtype": "Data", "width": 130},
        {"label": _("Status"), "fieldname": "status", "fieldtype": "Data", "width": 110},
        {"label": _("Payment"), "fieldname": "payment_status", "fieldtype": "Data", "width": 110},
        {"label": _("Grand Total"), "fieldname": "grand_total", "fieldtype": "Currency", "width": 120},
        {"label": _("Coupon"), "fieldname": "coupon_code", "fieldtype": "Data", "width": 110},
        {"label": _("Age (hrs)"), "fieldname": "age_hours", "fieldtype": "Float", "width": 90},
        {"label": _("Age Bucket"), "fieldname": "age_bucket", "fieldtype": "Data", "width": 110},
        {"label": _("Created"), "fieldname": "creation", "fieldtype": "Datetime", "width": 150},
    ]

    rows = frappe.get_all(
        "Order",
        filters={
            "payment_status": ("in", ["Unpaid", "Partially Paid"]),
            "status": ("not in", ["Cancelled", "Refunded"]),
        },
        fields=[
            "name", "customer_name", "customer_phone", "status",
            "payment_status", "grand_total", "coupon_code", "creation",
        ],
        order_by="creation desc",
        limit=500,
    )

    data = []
    bucket_totals = {}
    for r in rows:
        age_h = (now - r.creation).total_seconds() / 3600.0
        if age_h < hours:
            continue
        bucket = _bucket(age_h)
        bucket_totals.setdefault(bucket, {"count": 0, "value": 0.0})
        bucket_totals[bucket]["count"] += 1
        bucket_totals[bucket]["value"] += r.grand_total or 0
        data.append({
            **r,
            "age_hours": round(age_h, 1),
            "age_bucket": bucket,
        })

    # Summary rows: the recovery-relevant view.
    for bucket in ("0-6h", "6-24h", "1-3d", "3-7d", "7d+"):
        if bucket in bucket_totals:
            b = bucket_totals[bucket]
            data.append(frappe._dict({
                "customer_name": f"— {bucket}: {b['count']} orders —",
                "grand_total": b["value"],
            }))

    expired_carts = frappe.get_all(
        "Cart",
        filters={"status": "Active", "expires_at": ("<", now)},
        fields=["subtotal"],
        as_list=True,
    )
    cart_count = len(expired_carts)
    cart_value = sum(r[0] or 0 for r in expired_carts)
    if cart_count:
        data.append(frappe._dict({
            "customer_name": f"— Expired active carts: {cart_count} (value रु{cart_value or 0}) —",
        }))

    return columns, data


def _bucket(age_hours):
    if age_hours < 6:
        return "0-6h"
    if age_hours < 24:
        return "6-24h"
    if age_hours < 72:
        return "1-3d"
    if age_hours < 168:
        return "3-7d"
    return "7d+"
