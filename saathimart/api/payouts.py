"""
Vendor payouts API — how much is currently owed to a vendor, and recording
that a payout actually happened.

Money owed lives implicitly in Vendor Fulfillment rows: paid, non-cancelled
fulfillments whose `vendor_payout` link is still empty. Vendor Payout
records that link (see saathimart.saathimart.doctype.vendor_payout) so an
amount is only ever counted as owed once.
"""
import frappe
from frappe import _
from frappe.utils import flt


def _require_vendor_access(vendor):
    """SM Admin can act on any vendor; SM Vendor only on themselves."""
    roles = frappe.get_roles()
    if "SM Admin" in roles:
        return
    if "SM Vendor" in roles:
        self_vendor = frappe.db.get_value(
            "Vendor", {"contact_email": frappe.session.user}, "name"
        )
        if self_vendor and self_vendor == vendor:
            return
    frappe.throw(_("Not permitted"), frappe.PermissionError)


@frappe.whitelist()
def get_outstanding_payout(vendor):
    """
    How much the platform currently owes `vendor` right now — every paid,
    non-cancelled Vendor Fulfillment that hasn't been claimed by a Vendor
    Payout yet, across all time (not bounded to a report date range).
    """
    _require_vendor_access(vendor)

    commission_pct = flt(frappe.db.get_value("Vendor", vendor, "commission_pct"))
    row = frappe.db.sql("""
        SELECT COUNT(DISTINCT vf.parent) as order_count, SUM(vf.subtotal) as gross_sales
        FROM `tabVendor Fulfillment` vf
        INNER JOIN `tabOrder` o ON o.name = vf.parent
        WHERE vf.vendor = %(vendor)s
          AND vf.status != 'Cancelled'
          AND (vf.vendor_payout IS NULL OR vf.vendor_payout = '')
          AND o.payment_status = 'Paid'
          AND o.status != 'Cancelled'
    """, {"vendor": vendor}, as_dict=True)[0]

    gross = flt(row.gross_sales)
    commission = gross * commission_pct / 100
    return {
        "vendor": vendor,
        "commission_pct": commission_pct,
        "order_count": row.order_count or 0,
        "gross_sales": gross,
        "commission_amount": commission,
        "payout_due": gross - commission,
    }


@frappe.whitelist()
def create_vendor_payout(vendor, from_date, to_date, payment_reference="", notes=""):
    """
    Record that `vendor` was actually paid for their unsettled, paid
    fulfillments within [from_date, to_date]. Admin-only — this is a record
    of money that has already moved, not a request to pay.
    """
    if "SM Admin" not in frappe.get_roles():
        frappe.throw(_("Not permitted"), frappe.PermissionError)

    doc = frappe.new_doc("Vendor Payout")
    doc.vendor = vendor
    doc.from_date = from_date
    doc.to_date = to_date
    doc.payment_reference = payment_reference or ""
    doc.notes = notes or ""
    doc.insert(ignore_permissions=True)
    frappe.db.commit()

    return {
        "payout_id": doc.name,
        "gross_sales": doc.gross_sales,
        "commission_amount": doc.commission_amount,
        "payout_amount": doc.payout_amount,
        "fulfillments_count": doc.fulfillments_count,
    }


@frappe.whitelist()
def list_vendor_payouts(vendor=None, page=1, page_size=20):
    filters = {}
    roles = frappe.get_roles()
    if "SM Admin" not in roles:
        if "SM Vendor" not in roles:
            frappe.throw(_("Not permitted"), frappe.PermissionError)
        self_vendor = frappe.db.get_value(
            "Vendor", {"contact_email": frappe.session.user}, "name"
        )
        filters["vendor"] = self_vendor or ["in", []]
    elif vendor:
        filters["vendor"] = vendor

    page      = max(1, int(page))
    page_size = min(100, max(1, int(page_size)))

    return frappe.get_list(
        "Vendor Payout",
        filters=filters,
        fields=["name", "vendor", "payout_date", "from_date", "to_date",
                "gross_sales", "commission_amount", "payout_amount",
                "fulfillments_count", "payment_reference"],
        limit_start=(page - 1) * page_size,
        limit=page_size,
        order_by="payout_date desc",
    )
