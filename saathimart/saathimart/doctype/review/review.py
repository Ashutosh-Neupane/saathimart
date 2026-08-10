import frappe
from frappe import _
from frappe.model.document import Document


class Review(Document):
    def before_insert(self):
        if not self.reviewer_name and self.user:
            self.reviewer_name = frappe.db.get_value("User", self.user, "full_name")

    def validate(self):
        if not (1 <= (self.rating or 0) <= 5):
            frappe.throw(_("Rating must be between 1 and 5"))
