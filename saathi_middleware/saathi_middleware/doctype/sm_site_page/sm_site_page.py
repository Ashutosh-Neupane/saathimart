import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime


class SMSitePage(Document):
    def before_save(self):
        if not self.slug:
            self.slug = frappe.scrub(self.title).replace("_", "-")
        if self.status == "Published" and not self.published_at:
            self.published_at = now_datetime()

    def on_update(self):
        frappe.cache().delete_key(f"sm_page:{self.slug}")
