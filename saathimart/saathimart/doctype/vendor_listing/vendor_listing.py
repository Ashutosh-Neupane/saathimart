import frappe
from frappe.model.document import Document


class VendorListing(Document):
    def validate(self):
        if self.sync_enabled and not self.barcode:
            frappe.throw("A barcode is required to enable sync. Set the barcode first, then enable sync.")

    def before_save(self):
        # If this listing has a barcode and sync_enabled, auto-set sync_status to Pending
        if self.sync_enabled and self.barcode and not self.sync_status:
            self.sync_status = "Pending"
