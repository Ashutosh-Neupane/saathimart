from frappe.model.document import Document


class SaathiMartSettings(Document):
    def on_update(self):
        # api.settings.get_settings() caches this Single doc in Redis for
        # 5 minutes (perf — it's read on nearly every request). Without
        # this, a Desk save sits stale in cache for up to that long: an
        # admin flips enable_esewa or edits esewa_merchant_code and
        # checkout keeps reading the old value until the TTL happens to
        # expire.
        from saathimart.api.settings import invalidate_settings_cache
        invalidate_settings_cache()
        # Storefront-revalidation settings are cached separately (module keeps
        # its own 5-min redis copy of base URL / secret / enable flag).
        from saathimart.api.storefront_cache import clear_settings_cache
        clear_settings_cache()
