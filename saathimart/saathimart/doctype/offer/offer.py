import frappe
from frappe.model.document import Document


class Offer(Document):
    def before_save(self):
        if not self.slug:
            self.slug = frappe.scrub(self.title).replace("_", "-")

    def on_update(self):
        frappe.cache().delete_key("sm_offers:all")
        if self.slug:
            frappe.cache().delete_key(f"sm_offer:{self.slug}")
