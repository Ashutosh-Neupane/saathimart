import frappe
from frappe.model.document import Document


class FAQCategory(Document):
    def before_save(self):
        if not self.slug:
            self.slug = frappe.scrub(self.category_name).replace("_", "-")

    def on_update(self):
        frappe.cache().delete_key("sm_faq_categories")
        frappe.cache().delete_key("sm_faq:all")
