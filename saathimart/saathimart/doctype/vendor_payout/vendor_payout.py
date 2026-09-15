import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, getdate, rounded


class VendorPayout(Document):
    def validate(self):
        if self.period_start and self.period_end:
            if getdate(self.period_start) > getdate(self.period_end):
                frappe.throw(_("Period start must be before period end"))

    def before_save(self):
        if not self.vendor_name and self.vendor:
            self.vendor_name = frappe.db.get_value("Vendor", self.vendor, "vendor_name") or ""

        # Auto-calculate commission from the platform-wide rate
        # (SaathiMart Settings.default_commission_pct) — commission is the
        # platform's charge to every vendor, not a per-vendor field.
        if self.vendor:
            from saathimart.api.commission import get_commission_pct_for_vendor
            commission_pct = get_commission_pct_for_vendor(self.vendor)
            self.commission_pct = commission_pct
            self.commission_amount = rounded(flt(self.total_sales) * flt(commission_pct) / 100, 2)
            # Net of the platform's 13% service VAT on its commission bill —
            # the platform collects that VAT and remits it to IRD; it was
            # never part of the vendor's receivable (whose sale GL is booked
            # at the full product base). Matches generate_settlement_statement.
            service_vat = rounded(self.commission_amount * 13.0 / 100.0, 2)
            self.payout_amount = rounded(
                flt(self.total_sales) - self.commission_amount - service_vat, 2
            )

    @frappe.whitelist()
    def calculate_payout(self):
        """
        Populate orders and totals from this vendor's outstanding
        fulfillments in the period — same definition of "outstanding"
        api/payouts.py:get_outstanding_payout reports (paid order,
        non-cancelled order/fulfillment, not already claimed by another
        payout), not narrowed to status='Delivered' — a payout must settle
        exactly what get_outstanding_payout said was owed, or the two
        permanently disagree.

        Claims each included fulfillment by stamping its vendor_payout
        link to this payout's name — without this, calling calculate_payout
        twice for the same vendor/period double-counts the same
        fulfillments into two payouts. on_trash below reverses the claim.
        """
        if not self.vendor or not self.period_start or not self.period_end:
            frappe.throw(_("Vendor, period start, and period end are required"))

        fulfillments = frappe.db.sql("""
            SELECT vf.name, vf.subtotal, o.name as order_id, o.customer_name,
                   vf.modified as delivered_at
            FROM `tabVendor Fulfillment` vf
            INNER JOIN `tabOrder` o ON vf.parent = o.name
            WHERE vf.vendor = %s
              AND vf.status != 'Cancelled'
              AND (vf.vendor_payout IS NULL OR vf.vendor_payout = '')
              AND o.payment_status = 'Paid'
              AND o.status != 'Cancelled'
              AND DATE(vf.modified) BETWEEN %s AND %s
        """, (self.vendor, self.period_start, self.period_end), as_dict=True)

        if not fulfillments:
            frappe.throw(_("Nothing outstanding to pay {0} for this period").format(self.vendor))

        # Track claimed fulfillments and accumulate sales total.
        self.total_sales = 0
        orders_count = 0
        for f in fulfillments:
            self.total_sales = flt(self.total_sales) + flt(f.subtotal)
            orders_count += 1

        self.fulfillments_count = orders_count
        self.save(ignore_permissions=True)

        for f in fulfillments:
            frappe.db.set_value("Vendor Fulfillment", f.name, "vendor_payout", self.name)

        return {
            "orders_count": orders_count,
            "total_sales": self.total_sales,
            "payout_amount": self.payout_amount,
        }

    def on_trash(self):
        """Release this payout's fulfillments so they're outstanding again.

        fulfillments_count (an Int mirror of the now-deleted child table) is
        cleared to keep the record honest on re-calculation.
        """
        frappe.db.set_value(
            "Vendor Fulfillment", {"vendor_payout": self.name}, "vendor_payout", None
        )
        frappe.db.set_value(self.doctype, self.name, "fulfillments_count", 0)
