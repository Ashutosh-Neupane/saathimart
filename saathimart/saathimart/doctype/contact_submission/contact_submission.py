import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime


class ContactSubmission(Document):
    def before_insert(self):
        self.submitted_at = now_datetime()
        if not self.status:
            self.status = "New"
