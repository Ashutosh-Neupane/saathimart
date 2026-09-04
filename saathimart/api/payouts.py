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
from saathimart.api.responses import handle_api_errors


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
@handle_api_errors
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
@handle_api_errors
def create_vendor_payout(vendor, from_date, to_date, payment_reference="", notes=""):
    """
    Record that `vendor` was actually paid for their unsettled, paid
    fulfillments within [from_date, to_date]. Admin-only — this is a record
    of money that has already moved, not a request to pay.

    Regression note: this used to set doc.from_date/doc.to_date and read
    back doc.gross_sales/doc.fulfillments_count — none of which exist on
    Vendor Payout (real fields: period_start, period_end, total_sales, the
    orders child table). Every call failed on the missing-mandatory-field
    validation for period_start/period_end before it could even reach the
    fields that don't exist. calculate_payout() (see the doctype controller)
    already has the real logic to populate totals and claim fulfillments —
    this just needed to actually call it.
    """
    if "SM Admin" not in frappe.get_roles():
        frappe.throw(_("Not permitted"), frappe.PermissionError)

    doc = frappe.new_doc("Vendor Payout")
    doc.vendor = vendor
    doc.period_start = from_date
    doc.period_end = to_date
    doc.payment_reference = payment_reference or ""
    doc.notes = notes or ""
    doc.insert(ignore_permissions=True)
    try:
        doc.calculate_payout()  # populates orders/total_sales, claims fulfillments, throws if nothing outstanding
    except Exception:
        frappe.delete_doc("Vendor Payout", doc.name, ignore_permissions=True, force=True)
        raise
    frappe.db.commit()

    # Create settlement Journal Entry in accounting
    try:
        from saathimart.api.accounting import create_settlement_journal_entry, generate_settlement_statement
        statement = generate_settlement_statement(vendor, from_date, to_date)
        create_settlement_journal_entry(
            vendor_name=vendor,
            payout_id=doc.name,
            amount=doc.payout_amount,
            commission=doc.commission_amount,
            coupon_reimbursement=statement.get("platform_coupon_discount", 0),
            loyalty_reimbursement=statement.get("loyalty_discount", 0),
        )
        # Publish settlement.completed to vendor so they can create their
        # own Journal Entry (Bank debit, Commission expense, Clearing credit)
        from saathimart.events.publisher import publish_settlement
        publish_settlement(
            vendor_name=vendor,
            payout_id=doc.name,
            amount=doc.payout_amount,
            commission=doc.commission_amount,
            coupon_reimbursement=statement.get("platform_coupon_discount", 0),
            loyalty_reimbursement=statement.get("loyalty_discount", 0),
        )
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            f"Settlement Journal Entry failed for payout {doc.name}"
        )

    return {
        "payout_id": doc.name,
        "gross_sales": doc.total_sales,
        "commission_amount": doc.commission_amount,
        "payout_amount": doc.payout_amount,
        "fulfillments_count": len(doc.orders),
    }


@frappe.whitelist()
@handle_api_errors
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
        fields=["name", "vendor", "creation", "period_start", "period_end",
                "total_sales", "commission_amount", "payout_amount",
                "payment_reference"],
        limit_start=(page - 1) * page_size,
        limit=page_size,
        order_by="creation desc",
    )
