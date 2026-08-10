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
        return frappe.db.get_value(
            "Vendor Stock",
            {"vendor": self.name},
            ["SUM(available_qty) AS total_available", "SUM(physical_qty) AS total_physical"],
            as_dict=True,
        ) or {"total_available": 0, "total_physical": 0}
