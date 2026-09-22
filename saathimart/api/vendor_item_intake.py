"""Vendor-item intake — the target of the ERPNext Item "Sync to Saathi" button.

A vendor clicks Sync on their ERPNext Item; saathimart_vendor.api.item_sync
posts the item here (HMAC-signed like every other vendor push) and this
endpoint makes the item fully sellable on the hub in one shot:

  1. Category / Brand masters upserted from the item's Item Group / Brand.
  2. Product upserted (matched by barcode first via Vendor Listing / Product
     SKU, then created).
  3. Vendor Listing upserted with the vendor's own price (the storefront
     price) — the canonical writer products.create_vendor_listing.
  4. Vendor Stock row ensured and the vendor's qty applied as a stock.receipt
     delta (the canonical writer stock.apply_vendor_stock_event — same
     validation, SLE and idempotency as any other stock push).
  5. Vendor Barcode Index registered so future hub products with this
     barcode auto-notify this vendor (events._apply_barcode_register).

Every step goes through the canonical writer instead of raw inserts so the
intake path can never drift from the event paths.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt

from saathimart.api.responses import handle_api_errors
from saathimart.api.utils import guest_rate_limit, verify_hub_secret


def _slugify(value: str) -> str:
	import re
	value = (value or "").strip().lower()
	value = re.sub(r"[^a-z0-9\u0900-\u097f]+", "-", value).strip("-")
	return value or frappe.generate_hash(length=8)


def _ensure_category(item_group: str) -> str | None:
	if not item_group:
		return None
	existing = frappe.db.get_value("Category", {"category_name": item_group}, "name")
	if existing:
		return existing
	doc = frappe.new_doc("Category")
	doc.category_name = item_group
	doc.slug = _slugify(item_group)
	doc.is_active = 1
	doc.insert(ignore_permissions=True)
	return doc.name


def _ensure_brand(brand_name: str) -> str | None:
	if not brand_name:
		return None
	existing = frappe.db.get_value("Brand", {"brand_name": brand_name}, "name")
	if existing:
		return existing
	doc = frappe.new_doc("Brand")
	doc.brand_name = brand_name
	doc.slug = _slugify(brand_name)
	doc.is_active = 1
	doc.insert(ignore_permissions=True)
	return doc.name


def _resolve_product(barcode: str, item_name: str, category, brand,
					 specifications=None) -> str:
	"""Match an existing hub product by barcode (listing first, then legacy
	Product.sku), otherwise create one.

	`specifications` is the vendor Item's structured Website Specifications
	([{label, value}, ...]) — stored on the Product so the storefront PDP can
	render a spec table. New products get them on create; an existing product
	matched by barcode gets them only when it has none yet (first sync wins,
	so repeated button pushes can't clobber curated content).
	"""
	spec_rows = [
		{"label": (r.get("label") or "").strip(), "value": (r.get("value") or "").strip()}
		for r in (specifications or [])
		if isinstance(r, dict) and (r.get("label") or "").strip()
	]

	if barcode:
		via_listing = frappe.db.get_value("Vendor Listing", {"barcode": barcode}, "product")
		if via_listing:
			if spec_rows and not frappe.db.count(
				"Product Specification", {"parent": via_listing, "parenttype": "Product"}
			):
				_fulfill_specs(via_listing, spec_rows)
			return via_listing
		via_sku = frappe.db.get_value("Product", {"sku": barcode}, "name")
		if via_sku:
			if spec_rows and not frappe.db.count(
				"Product Specification", {"parent": via_sku, "parenttype": "Product"}
			):
				_fulfill_specs(via_sku, spec_rows)
			return via_sku

	doc = frappe.new_doc("Product")
	doc.product_name = item_name or barcode or frappe.generate_hash(length=6)
	doc.slug = _slugify(doc.product_name)
	doc.status = "Active"
	doc.sku = barcode or None
	if category:
		doc.category = category
	if brand:
		doc.brand = brand
	for row in spec_rows:
		doc.append("specifications", row)
	doc.insert(ignore_permissions=True)
	return doc.name


def _fulfill_specs(product: str, spec_rows: list) -> None:
	"""Backfill specification rows onto an existing Product (no-overwrite
	semantics — callers check emptiness first)."""
	doc = frappe.get_doc("Product", product)
	for row in spec_rows:
		doc.append("specifications", row)
	doc.save(ignore_permissions=True)


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def register_item(item_code=None, item_name=None, barcode=None, price=0,
                  qty=0, category=None, brand=None, description=None, uom=None,
                  specifications=None):
	"""Upsert one vendor ERPNext Item into the hub catalog + listing + stock.

	Called by saathimart_vendor.api.item_sync.sync_item_to_saathi (the Item
	form's "Sync to Saathi" button). Authenticated with the same
	X-SM-Signature HMAC as every vendor push (verify_hub_secret).
	"""
	guest_rate_limit("vendor_item_intake.register_item", limit=120, window_seconds=60)
	verify_hub_secret("vendor_item_intake.register_item")

	vendor = (frappe.request.headers.get("X-Vendor-ID", "") if frappe.request else "") \
		or (frappe.get_request_header("X-Vendor-ID") or "")
	if not vendor or not frappe.db.exists("Vendor", vendor):
		frappe.throw(_("Unknown vendor (X-Vendor-ID header)"), frappe.AuthenticationError)

	barcode = (barcode or "").strip()
	item_name = (item_name or "").strip() or item_code
	price = flt(price)
	qty = flt(qty)

	category_name = _ensure_category((category or "").strip())
	brand_name = _ensure_brand((brand or "").strip())
	product = _resolve_product(barcode, item_name, category_name, brand_name,
							   specifications=specifications)

	# 3. Listing with the vendor's own price — canonical writer.
	from saathimart.api.products import create_vendor_listing
	listing = create_vendor_listing(
		product=product,
		vendor=vendor,
		price=price,
		barcode=barcode,
		sku=item_code or "",
		status="Active",
		track_inventory=1,
		available_qty=qty,
	)

	# 4. Stock truth — canonical stock.receipt writer. The push is the
	# DELTA between the vendor's reported qty and the hub's current number,
	# so clicking the button repeatedly reconciles instead of stacking +qty
	# on every click (the stock event pipeline is delta-based and keyed on
	# event ids — a set-value semantic must be expressed as a delta here).
	if barcode:
		current = frappe.db.get_value(
			"Vendor Stock", {"vendor": vendor, "product": product}, "available_qty"
		)
		delta = flt(qty) - flt(current or 0)
		if abs(delta) > 0.0001:
			from saathimart.api.stock import apply_vendor_stock_event
			# qty_change is SIGNED — _apply_stock_delta adds it to the current
			# qty (stock.deduct does not negate; its callers pass negatives).
			apply_vendor_stock_event("stock.receipt" if delta > 0 else "stock.deduct", {
				"barcode": barcode,
				"hub_product": product,
				"qty_change": delta,
				"vendor_id": vendor,
				"voucher_no": item_code or "",
				"source_site": vendor,
				"remarks": f"Item sync from {vendor} (manual button push)",
			})

	# 5. Barcode index — same internal writer the barcode.register event uses.
	if barcode:
		from saathimart.api.events import _apply_barcode_register
		_apply_barcode_register({"vendor": vendor, "barcode": barcode})

	frappe.db.commit()
	return {
		"ok": True,
		"product": product,
		"listing": (listing or {}).get("name"),
		"listing_created": (listing or {}).get("created"),
		"qty_pushed": qty,
	}
