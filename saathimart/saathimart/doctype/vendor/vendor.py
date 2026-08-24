import frappe
from frappe.model.document import Document
from frappe.utils import flt, now_datetime


class Vendor(Document):
    def before_save(self):
        if not self.slug:
            self.slug = frappe.scrub(self.vendor_name).replace("_", "-")

    def on_update(self):
        frappe.cache().delete_key(f"sm_vendor:{self.name}")
        frappe.cache().delete_key("sm_vendor_list")

    def after_insert(self):
        self._recalculate_stock_totals()

    def on_trash(self):
        frappe.cache().delete_key(f"sm_vendor:{self.name}")
        frappe.cache().delete_key("sm_vendor_list")

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
