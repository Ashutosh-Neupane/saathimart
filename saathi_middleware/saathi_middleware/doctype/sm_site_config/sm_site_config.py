import frappe
from frappe.model.document import Document


class SMSiteConfig(Document):
    def on_update(self):
        frappe.cache().delete_key("sm_site_config")
