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

        # Compute stock_qty from Vendor Stock (source of truth)
        self._sync_stock_from_vendors()

    def validate(self):
        if self.price and self.price < 0:
            frappe.throw("Price cannot be negative")
        self._validate_variants()

    def _validate_variants(self):
        if self.has_variants and self.variant_of:
            frappe.throw("A product cannot both have variants and be a variant of another product")
        if self.variant_of:
            if self.variant_of == self.name:
                frappe.throw("A product cannot be a variant of itself")
            parent_has_variants, parent_status = frappe.db.get_value(
                "Product", self.variant_of, ["has_variants", "status"]
            ) or (0, None)
            if parent_status is None:
                frappe.throw(f"Variant Of product {self.variant_of} does not exist")
            if not parent_has_variants:
                frappe.throw(
                    f"{self.variant_of} is not marked Has Variants — "
                    "it can't be used as a variant template"
                )

    def on_update(self):
        frappe.cache().delete_key(f"sm_product:{self.name}")
        frappe.cache().delete_key("sm_product_list")

    def _get_listings(self):
        return frappe.get_list(
            "Vendor Listing",
            filters={"product": self.name, "status": "Active"},
            fields=["vendor", "price", "compare_price", "track_inventory",
                    "allow_backorder", "available_qty", "reserved_qty",
                    "sku", "vendor_product_id", "delivery_zone",
                    "barcode", "status"],
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

    # stock_qty, track_inventory, and sku are deliberately NOT properties
    # here, unlike vendor/price/compare_price/etc. below — those have no
    # backing column at all (removed from product.json by the v1_to_v2
    # migration), so a property is the only way to read them and there's
    # no ambiguity. stock_qty/track_inventory/sku *do* still have a real
    # stored column (product.json), used directly by real code:
    # saathimart.saathimart.doctype.stock_ledger_entry.stock_ledger_entry.make_entry()
    # (the legacy pooled-stock fallback for stock events with no vendor_id),
    # lookup_by_barcode()'s legacy fallback, and
    # events.publisher.on_product_created's barcode broadcast.
    #
    # A @property with the same name as a real field is a Python data
    # descriptor — it always wins over instance __dict__ on `doc.attr`
    # access, REGARDLESS of what's actually stored in the database. That
    # was live here: doc.sku on a freshly-inserted Product always returned
    # "" (no Vendor Listing can exist yet for a brand-new product), even
    # though the admin's typed SKU had been correctly written to the raw
    # column — confirmed live, `frappe.db.get_value(..., "sku")` returned
    # the real value while `doc.sku` returned "" for the exact same row.
    # That silently broke on_product_created's entire barcode-matching
    # path — see saathimart/events/publisher.py — since every hook there
    # reads `doc.sku` on the just-inserted Document, not a raw query.

    def _sync_stock_from_vendors(self):
        """Compute stock_qty as the sum of all active Vendor Stock rows.

        The Product's stock_qty is a read-only aggregate — the source of truth
        is Vendor Stock (per vendor+product+warehouse). This method recalculates
        it on every save so the product page can show a single stock number
        without querying the stock table.
        """
        total = frappe.db.sql("""
            SELECT COALESCE(SUM(available_qty), 0) AS total
            FROM `tabVendor Stock`
            WHERE product = %s
        """, self.name, as_dict=True)
        self.stock_qty = flt(total[0].total) if total else 0

    @property
    def allow_backorder(self):
        return any(l.allow_backorder for l in self._get_listings())

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
