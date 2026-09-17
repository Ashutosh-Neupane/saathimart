import frappe
from frappe.model.document import Document


class Brand(Document):
    def before_save(self):
        if not self.slug:
            self.slug = frappe.scrub(self.brand_name or "").replace("_", "-")

    def on_update(self):
        # Single source of truth for brand-derived invalidation: the
        # storefront_cache layer (doc_events also fire on_brand_changed for
        # this doctype). This controller hook predates that layer.
        from saathimart.api.storefront_cache import bust_brand_cache
        bust_brand_cache()
        # Product detail responses embed the resolved brand name — clear
        # them so a rename shows up immediately.
        from saathimart.api.storefront_cache import _delete_pattern
        _delete_pattern("sm_product:*")
