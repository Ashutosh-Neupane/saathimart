import frappe
from frappe.model.document import Document


class Banner(Document):
    def on_update(self):
        frappe.cache().delete_key("sm_banners")
