import frappe
from frappe.model.document import Document
from frappe.utils import add_days, now_datetime, flt


class Cart(Document):
    def before_insert(self):
        if not self.expires_at:
            self.expires_at = add_days(now_datetime(), 7)

    def validate(self):
        self.subtotal = sum(
            (item.qty or 0) * (item.rate or 0) for item in self.items
        )
        for item in self.items:
            item.amount = (item.qty or 0) * (item.rate or 0)

        # Validation: Cart cannot be empty
        if not self.items or len(self.items) == 0:
            frappe.throw(_("Cart must contain at least one item"), title="Empty Cart")

        # Validation: All items must have positive quantity and rate
        for item in self.items:
            if flt(item.qty) <= 0:
                frappe.throw(_("Item '{0}' must have a positive quantity").format(item.product), title="Invalid Quantity")
            if flt(item.rate) < 0:
                frappe.throw(_("Item '{0}' must have a non-negative rate").format(item.product), title="Invalid Rate")

    def before_save(self):
        # Update subtotal before saving
        self.subtotal = sum(flt(item.qty) * flt(item.rate) for item in self.items or [])
