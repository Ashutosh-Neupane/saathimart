import frappe
from frappe.model.document import Document


class PaymentMode(Document):
    def validate(self):
        if not self.slug:
            self.slug = frappe.scrub(self.mode_name).replace("_", "-")
