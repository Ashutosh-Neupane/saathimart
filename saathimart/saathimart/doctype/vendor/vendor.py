import frappe
import secrets
from frappe.model.document import Document
from frappe.utils import flt, now_datetime
from frappe.utils.password import set_encrypted_password


class Vendor(Document):
    def before_save(self):
        # Auto-generate API key on first creation (plain text, no save needed)
        if not self.api_key:
            self.api_key = secrets.token_urlsafe(16)

        if not self.slug:
            self.slug = frappe.scrub(self.vendor_name).replace("_", "-")

        # Validate only one default warehouse
        defaults = [w for w in (self.warehouses or []) if w.is_default]
        if len(defaults) > 1:
            frappe.throw(
                "Only one warehouse can be marked as default. "
                "{0} and {1} are both set as default.".format(
                    defaults[0].warehouse_name, defaults[1].warehouse_name
                )
            )

        # Sync read-only default_warehouse_name field
        if defaults:
            self.default_warehouse_name = defaults[0].warehouse_name
        else:
            self.default_warehouse_name = self.default_warehouse or ""

    def on_update(self):
        frappe.cache().delete_key(f"sm_vendor:{self.name}")
        frappe.cache().delete_key("sm_vendor_list")

    def after_insert(self):
        # Auto-generate secrets after doc is saved (set_password requires docname)
        self._generate_secrets_if_missing()
        self._recalculate_stock_totals()
        self._populate_credentials_info()

    def on_trash(self):
        frappe.cache().delete_key(f"sm_vendor:{self.name}")
        frappe.cache().delete_key("sm_vendor_list")

    def _generate_secrets_if_missing(self):
        """Generate webhook_secret and api_secret if not already set.
        Must run after_insert (not before_save) because set_encrypted_password
        requires the doc to already have a name in the DB.

        Checks the live __Auth table, not `self.webhook_secret` — a Password
        field always reads as None on an unsaved/in-memory doc, so checking
        the attribute would make this clobber secrets that
        api/vendor_onboarding.register_vendor writes right after insert.
        No frappe.db.commit() here: after_insert runs inside the outer
        INSERT transaction, and committing mid-event breaks its atomicity.
        """
        from frappe.utils.password import get_decrypted_password

        if not get_decrypted_password("Vendor", self.name, "webhook_secret", raise_exception=False):
            set_encrypted_password("Vendor", self.name, secrets.token_urlsafe(32), "webhook_secret")
        if not get_decrypted_password("Vendor", self.name, "api_secret", raise_exception=False):
            set_encrypted_password("Vendor", self.name, secrets.token_urlsafe(32), "api_secret")

    def _populate_credentials_info(self):
        """Show the admin what to copy to the vendor's Frappe site.

        Includes the freshly generated secrets — Password fields can't be
        read back from the form, so this creation-time snapshot is the only
        place the admin can copy them from (same "copy now" UX as one-time
        tokens). register_vendor's own (different) secrets overwrite the
        stored ones right after insert, so only trust this snapshot when
        it matches what's in __Auth.
        """
        from frappe.utils.password import get_decrypted_password

        api_key = self.api_key or frappe.db.get_value("Vendor", self.name, "api_key") or "(not set)"
        api_secret = get_decrypted_password("Vendor", self.name, "api_secret", raise_exception=False)
        webhook_secret = get_decrypted_password("Vendor", self.name, "webhook_secret", raise_exception=False)
        info = (
            "Copy these to the vendor's saathimart-vendor site:\n"
            "• Hub Site URL: {hub_url}\n"
            "• API Key: {api_key}\n"
            "• API Secret: {api_secret}\n"
            "• Webhook Secret: {webhook_secret}\n\n"
            "(Copy these now — the secrets are encrypted and cannot be\n"
            "viewed again from the form.)\n\n"
            "The vendor configures these in Vendor Config → Hub Connection."
        ).format(
            hub_url=frappe.utils.get_url(),
            api_key=api_key,
            api_secret=api_secret or "(not set)",
            webhook_secret=webhook_secret or "(not set)",
        )
        frappe.db.set_value("Vendor", self.name, "credentials_info", info, update_modified=False)

    def _recalculate_stock_totals(self):
        totals = frappe.db.sql("""
            SELECT
                COALESCE(SUM(available_qty), 0) AS total_available,
                COALESCE(SUM(physical_qty), 0) AS total_physical
            FROM `tabVendor Stock`
            WHERE vendor = %s
        """, self.name, as_dict=True)[0]
        frappe.db.set_value("Vendor", self.name, {
            "total_available_qty": flt(totals.total_available),
            "total_physical_qty": flt(totals.total_physical),
        }, update_modified=False)

    def get_stock_summary(self):
        # Plain row fetch + Python-side sums — dict-aggregate syntax
        # ({"SUM": ..., "as": ...}) crashes the v15 query engine.
        rows = frappe.get_all(
            "Vendor Stock",
            filters={"vendor": self.name},
            fields=["available_qty", "physical_qty"],
            limit_page_length=0,
        )
        return {
            "total_available": sum(flt(r.available_qty) for r in rows),
            "total_physical": sum(flt(r.physical_qty) for r in rows),
        }
