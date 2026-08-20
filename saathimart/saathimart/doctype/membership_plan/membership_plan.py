import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt


class MembershipPlan(Document):
    def validate(self):
        self.validate_benefits()

    def validate_benefits(self):
        """Reject rules that would silently never fire or would discount a line
        below zero — both are far cheaper to catch here than in a customer's
        basket, where the only symptom is a total nobody can explain."""
        seen = set()
        for row in self.benefits or []:
            if row.scope == "Category" and not row.category:
                frappe.throw(_("Row {0}: pick a Category, or change Applies To.").format(row.idx))
            if row.scope == "Brand" and not row.brand:
                frappe.throw(_("Row {0}: enter a Brand, or change Applies To.").format(row.idx))

            if flt(row.value) <= 0:
                frappe.throw(_("Row {0}: Value must be greater than zero.").format(row.idx))
            if row.discount_type == "Percentage" and flt(row.value) > 100:
                frappe.throw(_("Row {0}: a percentage discount cannot exceed 100.").format(row.idx))

            # Two active rules with the same target and the same priority have no
            # deterministic winner — the resolver would pick by row order, which
            # changes whenever someone drags the grid around.
            key = (row.scope, row.category or row.brand or "", row.priority)
            if row.is_active and key in seen:
                frappe.throw(
                    _("Row {0}: another active rule already targets the same thing at priority {1}. "
                      "Give one of them a different priority.").format(row.idx, row.priority)
                )
            seen.add(key)
