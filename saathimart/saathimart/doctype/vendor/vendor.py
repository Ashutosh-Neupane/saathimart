import frappe
import secrets
from frappe.model.document import Document
from frappe.utils import flt, now_datetime


class Vendor(Document):
    def before_save(self):
        # Auto-generate webhook secret on first creation
        if not self.webhook_secret and not self.get("_webhook_secret_generated"):
            self.set_password("webhook_secret", secrets.token_urlsafe(32))
            self.flags._webhook_secret_generated = True

        if not self.api_key:
            self.api_key = secrets.token_urlsafe(16)

        if not self.api_secret:
            self.set_password("api_secret", secrets.token_urlsafe(32))

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
        self._recalculate_stock_totals()
        # Populate setup instructions for the admin
        self._populate_credentials_info()

    def on_trash(self):
        frappe.cache().delete_key(f"sm_vendor:{self.name}")
        frappe.cache().delete_key("sm_vendor_list")

    def _populate_credentials_info(self):
        """Show the admin what to copy to the vendor's Frappe site."""
        site_url = self.frappe_site_url or "(not set yet)"
        api_key = self.api_key or "(not set)"
        info = (
            "Copy these to the vendor's saathimart-vendor site:\n"
            "• Hub Site URL: {hub_url}\n"
            "• API Key: {api_key}\n"
            "• API Secret: (shown on creation only — copy now!)\n"
            "• Webhook Secret: (shown on creation only — copy now!)\n\n"
            "The vendor configures these in Vendor Config → Hub Connection."
        ).format(
            hub_url=frappe.utils.get_url(),
            api_key=api_key,
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
