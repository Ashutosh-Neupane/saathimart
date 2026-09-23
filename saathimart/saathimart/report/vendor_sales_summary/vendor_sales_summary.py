"""Vendor Sales Summary — one row per vendor per period.

GMV, commission (via api/commission.get_commission_pct_for_vendor —
vendor override with Settings fallback), and fulfilment discipline
(delivered vs cancelled). Reads the same seam as accounting and payouts,
so ops can reconcile this report against Vendor Payouts directly.
"""
import frappe
from frappe.utils import add_days, flt, today


def execute(filters=None):
    filters = filters or {}
    from_date = filters.get("from_date") or add_days(today(), -30)
    to_date = filters.get("to_date") or today()

    from saathimart.api.commission import get_commission_pct_for_vendor

    rows = frappe.db.sql(
        """
        SELECT
            o.vendor AS vendor,
            COUNT(*) AS orders,
            SUM(CASE WHEN o.status = 'Delivered' THEN 1 ELSE 0 END) AS delivered,
            SUM(CASE WHEN o.status = 'Cancelled' THEN 1 ELSE 0 END) AS cancelled,
            SUM(o.grand_total) AS gmv,
            SUM(CASE WHEN o.payment_status = 'Paid' THEN o.grand_total ELSE 0 END) AS collected
        FROM `tabOrder` o
        WHERE o.docstatus < 2
          AND o.vendor IS NOT NULL AND o.vendor != ''
          AND DATE(o.creation) BETWEEN %s AND %s
        GROUP BY o.vendor
        ORDER BY gmv DESC
        """,
        (from_date, to_date),
        as_dict=True,
    )

    data = []
    for r in rows:
        pct = flt(get_commission_pct_for_vendor(r.vendor))
        commission = flt(r.gmv) * pct / 100
        data.append(
            {
                "vendor": r.vendor,
                "orders": r.orders,
                "delivered": r.delivered,
                "cancelled": r.cancelled,
                "gmv": flt(r.gmv),
                "collected": flt(r.collected),
                "commission_pct": pct,
                "commission": commission,
            }
        )

    columns = [
        {"label": "Vendor", "fieldname": "vendor", "fieldtype": "Link", "options": "Vendor", "width": 200},
        {"label": "Orders", "fieldname": "orders", "fieldtype": "Int", "width": 80},
        {"label": "Delivered", "fieldname": "delivered", "fieldtype": "Int", "width": 95},
        {"label": "Cancelled", "fieldname": "cancelled", "fieldtype": "Int", "width": 95},
        {"label": "GMV", "fieldname": "gmv", "fieldtype": "Currency", "width": 140},
        {"label": "Collected (Paid)", "fieldname": "collected", "fieldtype": "Currency", "width": 140},
        {"label": "Commission %", "fieldname": "commission_pct", "fieldtype": "Percent", "width": 115},
        {"label": "Commission", "fieldname": "commission", "fieldtype": "Currency", "width": 130},
    ]
    return columns, data
