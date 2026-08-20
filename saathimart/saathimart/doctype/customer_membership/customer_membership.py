import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_days, getdate, nowdate


class CustomerMembership(Document):
    def validate(self):
        if not self.started_on:
            self.started_on = nowdate()
        if not self.expires_on:
            days = frappe.db.get_value("Membership Plan", self.plan, "duration_days") or 365
            self.expires_on = add_days(self.started_on, int(days))

        if getdate(self.expires_on) < getdate(self.started_on):
            frappe.throw(_("Expiry date cannot be before the start date."))

        # A membership that has run out is Expired regardless of what the form
        # says, so a stale row can never keep granting discounts.
        if self.status == "Active" and getdate(self.expires_on) < getdate(nowdate()):
            self.status = "Expired"

    def before_insert(self):
        self.deactivate_overlapping()

    def deactivate_overlapping(self):
        """One active membership per customer. Buying a new plan supersedes the
        old one rather than stacking — the resolver reads a single membership,
        and two actives would make which-one-applies depend on row order."""
        existing = frappe.get_all(
            "Customer Membership",
            filters={"customer_email": self.customer_email, "status": "Active"},
            pluck="name",
        )
        for name in existing:
            frappe.db.set_value("Customer Membership", name, {
                "status": "Cancelled",
                "remarks": f"Superseded by a new membership on {nowdate()}",
            })
