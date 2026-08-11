import frappe
from frappe.utils import flt, today
from frappe.model.document import Document


class Product(Document):
    def before_save(self):
        if not self.slug:
            self.slug = frappe.scrub(self.product_name).replace("_", "-")

        primary = next((m for m in (self.media or []) if m.is_primary), None)
        if primary and primary.file:
            self.thumbnail = primary.file
        elif self.media and len(self.media) > 0:
            self.thumbnail = self.media[0].file

    def validate(self):
        if self.price and self.price < 0:
            frappe.throw("Price cannot be negative")

    def on_update(self):
        frappe.cache().delete_key(f"sm_product:{self.name}")
        frappe.cache().delete_key("sm_product_list")

    def _get_listings(self):
        return frappe.get_list(
            "Vendor Listing",
            filters={"product": self.name, "status": "Active"},
            fields=["vendor", "price", "compare_price", "track_inventory",
                    "allow_backorder", "available_qty", "reserved_qty",
                    "sku", "vendor_product_id", "delivery_zone"],
        )

    @property
    def vendor(self):
        listings = self._get_listings()
        return listings[0].vendor if listings else None

    @property
    def price(self):
        prices = [flt(l.price) for l in self._get_listings() if flt(l.price) > 0]
        return min(prices) if prices else 0

    @property
    def compare_price(self):
        prices = [flt(l.compare_price) for l in self._get_listings() if flt(l.compare_price) > 0]
        return max(prices) if prices else 0

    @property
    def stock_qty(self):
        total = 0
        for l in self._get_listings():
            total += flt(l.available_qty) + flt(l.reserved_qty)
        return total

    @property
    def track_inventory(self):
        return any(l.track_inventory for l in self._get_listings())

    @property
    def allow_backorder(self):
        return any(l.allow_backorder for l in self._get_listings())

    @property
    def sku(self):
        listings = self._get_listings()
        return listings[0].sku if listings else ""

    @property
    def barcode(self):
        listings = self._get_listings()
        return listings[0].barcode if listings else ""

    @property
    def vendor_product_id(self):
        listings = self._get_listings()
        return listings[0].vendor_product_id if listings else ""

    @property
    def delivery_zone(self):
        listings = self._get_listings()
        return listings[0].delivery_zone if listings else None

    def get_price_for(self, price_type="Site Price", qty=1, delivery_zone=None, vendor=None):
        if vendor:
            listing = next((l for l in self._get_listings() if l.vendor == vendor), None)
            if listing:
                return flt(listing.price)

        if delivery_zone:
            listing = next((l for l in self._get_listings() if l.delivery_zone == delivery_zone), None)
            if listing:
                return flt(listing.price)

        listing = next((l for l in self._get_listings() if l.status == "Active"), None)
        return flt(listing.price) if listing else flt(self.price)


def get_effective_price(product_doc, price_type="Site Price", qty=1,
                        delivery_zone=None, vendor=None):
    """Backward-compat import path — delegates to API module."""
    from saathimart.api.products import get_effective_price as _get_effective_price
    return _get_effective_price(product_doc, price_type=price_type, qty=qty,
                                delivery_zone=delivery_zone, vendor=vendor)
