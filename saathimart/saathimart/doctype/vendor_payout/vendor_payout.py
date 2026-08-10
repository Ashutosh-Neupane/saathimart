"""
Vendor Payout — records money actually transferred to a vendor.

Creating one marks every eligible Vendor Fulfillment row (paid order, not
cancelled, not already claimed by another payout) as settled by linking it
back to this record via Vendor Fulfillment.vendor_payout. That link is what
lets the commission report and get_outstanding_payout() tell already-paid
money apart from money still owed — deleting this record releases those
fulfillments so they become payable again.
"""
import frappe
from frappe.model.document import Document
from frappe.utils import flt, today


class VendorPayout(Document):
    def validate(self):
        if not self.payout_date:
            self.payout_date = today()
        if self.from_date and self.to_date and self.from_date > self.to_date:
            frappe.throw("Period From cannot be after Period To")

        if not self.is_new():
            return  # editing notes/reference on an already-recorded payout — totals are locked

        fulfillments = self._eligible_fulfillments()
        if not fulfillments:
            frappe.throw(
                "No paid, unsettled Vendor Fulfillments found for this vendor in the given period"
            )

        commission_pct = flt(frappe.db.get_value("Vendor", self.vendor, "commission_pct"))
        gross = sum(flt(f.subtotal) for f in fulfillments)

        self.gross_sales = gross
        self.commission_amount = gross * commission_pct / 100
        self.payout_amount = gross - self.commission_amount
        self.fulfillments_count = len(fulfillments)
        self._matched_fulfillment_names = [f.name for f in fulfillments]

    def _eligible_fulfillments(self):
        conditions = [
            "vf.vendor = %(vendor)s",
            "vf.status != 'Cancelled'",
            "(vf.vendor_payout IS NULL OR vf.vendor_payout = '')",
            "o.payment_status = 'Paid'",
            "o.status != 'Cancelled'",
        ]
        values = {"vendor": self.vendor}
        if self.from_date:
            conditions.append("DATE(o.creation) >= %(from_date)s")
            values["from_date"] = self.from_date
        if self.to_date:
            conditions.append("DATE(o.creation) <= %(to_date)s")
            values["to_date"] = self.to_date

        where = "WHERE " + " AND ".join(conditions)
        return frappe.db.sql(f"""
            SELECT vf.name, vf.subtotal
            FROM `tabVendor Fulfillment` vf
            INNER JOIN `tabOrder` o ON o.name = vf.parent
            {where}
        """, values, as_dict=True)

    def after_insert(self):
        for name in getattr(self, "_matched_fulfillment_names", None) or []:
            frappe.db.set_value("Vendor Fulfillment", name, "vendor_payout", self.name)

    def on_trash(self):
        frappe.db.set_value(
            "Vendor Fulfillment", {"vendor_payout": self.name}, "vendor_payout", ""
        )
