import frappe
from frappe.model.document import Document


class SMNavigationItem(Document):
    def on_update(self):
        frappe.cache().delete_key("sm_navigation")
