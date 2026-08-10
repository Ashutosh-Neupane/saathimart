import frappe
from frappe.utils import flt
from frappe.model.document import Document


class VendorStock(Document):
    def validate(self):
        # Keep physical_qty a straight sum of the two — the atomic reserve/
        # release/confirm helpers in api/stock.py update available_qty and
        # reserved_qty directly via SQL for race-safety, so this is the one
        # place that keeps physical_qty in sync for anything that saves the
        # document normally (desk edits, fixtures, etc).
        self.physical_qty = flt(self.available_qty) + flt(self.reserved_qty)
