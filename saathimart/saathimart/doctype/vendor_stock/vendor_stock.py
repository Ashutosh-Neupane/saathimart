import frappe
from frappe.utils import flt
from frappe.model.document import Document


class VendorStock(Document):
    def validate(self):
        # Enforce uniqueness: one Vendor Stock row per vendor+product+warehouse.
        # Warehouse is optional (empty = default warehouse for backward compat).
        if self.warehouse:
            existing = frappe.db.get_value(
                "Vendor Stock",
                {
                    "vendor": self.vendor,
                    "product": self.product,
                    "warehouse": self.warehouse,
                    "name": ("!=", self.name),
                },
                "name",
            )
            if existing:
                frappe.throw(
                    "Stock already tracked for vendor={0} product={1} warehouse={2} "
                    "(row: {3}). Duplicate not allowed.".format(
                        self.vendor, self.product, self.warehouse, existing
                    )
                )

        # Keep physical_qty a straight sum of the two — the atomic reserve/
        # release/confirm helpers in api/stock.py update available_qty and
        # reserved_qty directly via SQL for race-safety, so this is the one
        # place that keeps physical_qty in sync for anything that saves the
        # document normally (desk edits, fixtures, etc).
        self.physical_qty = flt(self.available_qty) + flt(self.reserved_qty)
