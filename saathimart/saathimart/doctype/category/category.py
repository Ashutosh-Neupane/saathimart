import frappe
from frappe.model.document import Document


class Category(Document):
    def before_save(self):
        if not self.slug:
            self.slug = frappe.scrub(self.category_name).replace("_", "-")
