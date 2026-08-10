"""
Vendor Commission Reconciliation — what the platform owes each vendor.

Aggregates by Vendor Fulfillment (not the legacy Order.vendor field) so
multi-vendor orders are split correctly across every vendor that fulfilled
part of them, instead of only the first vendor on the order.

Settled sales = fulfillments on Paid orders — commission is finalised on
these. Pending sales = fulfillments on Unpaid/Partially Paid orders — money
not yet collected, nothing to settle yet.

"Payout Due" is the number that matters operationally: settled sales whose
Vendor Fulfillment row hasn't been claimed by a Vendor Payout record yet
(see saathimart.saathimart.doctype.vendor_payout). Already Paid Out is
settled sales that HAVE been claimed — shown for reconciliation, not as
money still owed.
"""
import frappe
from frappe.utils import add_days, today, flt


def execute(filters=None):
    filters = filters or {}
    from_date = filters.get("from_date") or add_days(today(), -30)
    to_date = filters.get("to_date") or today()

    conditions = [
        "vf.status != 'Cancelled'",
        "o.status != 'Cancelled'",
        "DATE(o.creation) BETWEEN %(from_date)s AND %(to_date)s",
    ]
    values = {"from_date": from_date, "to_date": to_date}

    if filters.get("vendor"):
        conditions.append("vf.vendor = %(vendor)s")
        values["vendor"] = filters["vendor"]

    # SM Vendor users may only reconcile their own account, regardless of
    # the vendor filter passed in — this report exposes money owed.
    roles = frappe.get_roles()
    if "SM Admin" not in roles and "SM Vendor" in roles:
        self_vendor = frappe.db.get_value(
            "Vendor", {"contact_email": frappe.session.user}, "name"
        )
        conditions.append("vf.vendor = %(self_vendor)s")
        values["self_vendor"] = self_vendor or ""

    where = "WHERE " + " AND ".join(conditions)

    data = frappe.db.sql(f"""
        SELECT
            vf.vendor,
            v.vendor_name,
            v.commission_pct,
            COUNT(DISTINCT vf.parent) as order_count,
            SUM(vf.subtotal) as gross_sales,
            SUM(CASE WHEN o.payment_status = 'Paid' THEN vf.subtotal ELSE 0 END) as settled_sales,
            SUM(CASE WHEN o.payment_status != 'Paid' THEN vf.subtotal ELSE 0 END) as pending_sales,
            SUM(CASE WHEN o.payment_status = 'Paid'
                     AND vf.vendor_payout IS NOT NULL AND vf.vendor_payout != ''
                     THEN vf.subtotal ELSE 0 END) as paid_out_sales
        FROM `tabVendor Fulfillment` vf
        INNER JOIN `tabOrder` o ON o.name = vf.parent
        LEFT JOIN `tabVendor` v ON v.name = vf.vendor
        {where}
        GROUP BY vf.vendor
        ORDER BY gross_sales DESC
    """, values, as_dict=True)

    for row in data:
        commission_pct = flt(row.commission_pct)
        settled_sales = flt(row.settled_sales)
        paid_out_sales = flt(row.paid_out_sales)
        unpaid_settled_sales = settled_sales - paid_out_sales

        row.gross_sales = flt(row.gross_sales)
        row.settled_sales = settled_sales
        row.pending_sales = flt(row.pending_sales)
        row.commission_amount = settled_sales * commission_pct / 100
        row.already_paid_out = paid_out_sales * (1 - commission_pct / 100)
        row.payout_due = unpaid_settled_sales * (1 - commission_pct / 100)

    columns = [
        {"label": "Vendor", "fieldname": "vendor", "fieldtype": "Link", "options": "Vendor", "width": 150},
        {"label": "Name", "fieldname": "vendor_name", "fieldtype": "Data", "width": 180},
        {"label": "Orders", "fieldname": "order_count", "fieldtype": "Int", "width": 70},
        {"label": "Commission %", "fieldname": "commission_pct", "fieldtype": "Percent", "width": 100},
        {"label": "Gross Sales", "fieldname": "gross_sales", "fieldtype": "Currency", "width": 120},
        {"label": "Settled Sales", "fieldname": "settled_sales", "fieldtype": "Currency", "width": 120},
        {"label": "Pending Sales", "fieldname": "pending_sales", "fieldtype": "Currency", "width": 120},
        {"label": "Commission Amount", "fieldname": "commission_amount", "fieldtype": "Currency", "width": 140},
        {"label": "Already Paid Out", "fieldname": "already_paid_out", "fieldtype": "Currency", "width": 130},
        {"label": "Payout Due", "fieldname": "payout_due", "fieldtype": "Currency", "width": 120},
    ]

    return columns, data
