import frappe
from frappe.model.document import Document


class Brand(Document):
    def before_save(self):
        if not self.slug:
            self.slug = frappe.scrub(self.brand_name or "").replace("_", "-")

    def on_update(self):
        frappe.cache().delete_key("sm_brands_list")
        # Product list/detail responses embed the resolved brand name and
        # are cached per filter combination — clear them so a rename shows
        # up immediately.
        frappe.cache().delete_keys("sm_list_products:*")
        frappe.cache().delete_keys("sm_product:*")
