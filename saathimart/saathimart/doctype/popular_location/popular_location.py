import frappe
from frappe.model.document import Document


class PopularLocation(Document):
    def before_save(self):
        if not self.slug:
            self.slug = frappe.scrub(self.location_name).replace("_", "-")

    def on_update(self):
        frappe.cache().delete_key("sm_locations:all")
        frappe.cache().delete_key("sm_popular_cities")
        if hasattr(self, "city") and self.city:
            frappe.cache().delete_key(f"sm_locations:{self.city}")
