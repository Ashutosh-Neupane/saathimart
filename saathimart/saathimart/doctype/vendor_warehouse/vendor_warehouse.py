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
        # Only one default warehouse per vendor
        if self.is_default and self.parent and self.parenttype == "Vendor":
            existing = [
                r.name
                for r in frappe.get_all(
                    "Vendor Warehouse",
                    filters={
                        "parent": self.parent,
                        "parenttype": "Vendor",
                        "is_default": 1,
                        "name": ("!=", self.name),
                    },
                )
            ]
            if existing:
                frappe.throw(
                    "Vendor {0} already has a default warehouse ({1}). "
                    "Uncheck it first.".format(self.parent, existing[0])
                )
