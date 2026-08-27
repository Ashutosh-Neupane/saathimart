import frappe
from frappe.model.document import Document


class VendorWarehouse(Document):
    """A physical warehouse/location belonging to a vendor.

    Child table of Vendor. Used for:
    - Multi-location stock tracking (Vendor Stock gains a warehouse dimension)
    - Nearest-warehouse order routing (lat/lng used for distance calculation)
    - Per-location ERPNext warehouse mapping (erpnext_warehouse links to vendor's site)
    """

    def validate(self):
        # Default-warehouse uniqueness is validated in Vendor.before_save
        # (the parent doc) where all siblings are visible in memory.
        pass
