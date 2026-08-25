import frappe
from frappe.model.document import Document


class FAQItem(Document):
    def on_update(self):
        frappe.cache().delete_key("sm_faq_categories")
        frappe.cache().delete_key("sm_faq:all")
