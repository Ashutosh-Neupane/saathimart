import frappe
from frappe.model.document import Document
from frappe.utils import add_days, now_datetime


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
