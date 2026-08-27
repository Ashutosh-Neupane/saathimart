import frappe
from frappe import _
from frappe.utils import flt, getdate


class VendorPayout(Document):
    def validate(self):
        if self.period_start and self.period_end:
            if getdate(self.period_start) > getdate(self.period_end):
                frappe.throw(_("Period start must be before period end"))

    def before_save(self):
        if not self.vendor_name and self.vendor:
            self.vendor_name = frappe.db.get_value("Vendor", self.vendor, "vendor_name") or ""

        # Auto-calculate commission
        if self.vendor:
            commission_pct = frappe.db.get_value("Vendor", self.vendor, "commission_pct") or 0
            self.commission_pct = commission_pct
            self.commission_amount = flt(self.total_sales) * flt(commission_pct) / 100
            self.payout_amount = flt(self.total_sales) - flt(self.commission_amount)

    @frappe.whitelist()
    def calculate_payout(self):
        """Populate orders and totals from delivered Vendor Fulfillments."""
        if not self.vendor or not self.period_start or not self.period_end:
            frappe.throw(_("Vendor, period start, and period end are required"))

        # Find delivered fulfillments in the period
        fulfillments = frappe.db.sql("""
            SELECT vf.name, vf.subtotal, o.name as order_id, o.customer_name,
                   vf.modified as delivered_at
            FROM `tabVendor Fulfillment` vf
            INNER JOIN `tabOrder` o ON vf.parent = o.name
            WHERE vf.vendor = %s
              AND vf.status = 'Delivered'
              AND vf.modified BETWEEN %s AND %s
        """, (self.vendor, self.period_start, self.period_end), as_dict=True)

        self.orders = []
        self.total_sales = 0
        for f in fulfillments:
            self.append("orders", {
                "order_id": f.order_id,
                "customer_name": f.customer_name,
                "subtotal": f.subtotal,
                "delivered_at": f.delivered_at,
            })
            self.total_sales = flt(self.total_sales) + flt(f.subtotal)

        self.save(ignore_permissions=True)
        return {
            "orders_count": len(fulfillments),
            "total_sales": self.total_sales,
            "payout_amount": self.payout_amount,
        }
