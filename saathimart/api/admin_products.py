"""
Admin product/variant management API — lets a Next.js admin panel build
template + variant catalogs without desk access.

All endpoints require the SM Admin role. Everything is idempotent where it
can be: create_variants skips combinations that already exist, so a client
can re-submit its full matrix after adding one option value and only the
new combos get created.
"""
import json

import frappe
from frappe import _
from frappe.utils import flt

from saathimart.api.responses import handle_api_errors


def _require_admin():
    if "SM Admin" not in frappe.get_roles():
        frappe.throw(_("Not permitted"), frappe.PermissionError)


def _parse_json(value, field):
    """Accept a JSON string (querystring transport) or an already-parsed list."""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            frappe.throw(_("{0} must be valid JSON").format(field))
    if not isinstance(value, list):
        frappe.throw(_("{0} must be a JSON array").format(field))
    return value


def _slugify(value):
    import re
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-")


def _get_product_by_slug(slug):
    name = frappe.db.get_value("Product", {"slug": slug}, "name")
    if not name:
        frappe.throw(_("Product not found: {0}").format(slug), frappe.DoesNotExistError)
    return frappe.get_doc("Product", name)


# ── Templates ─────────────────────────────────────────────────────────────────


@frappe.whitelist()
@handle_api_errors
def create_template(product_name, options=None, category=None, brand=None,
                    short_description=None, description=None, thumbnail=None,
                    tags=None, slug=None):
    """
    Create a has_variants template Product from an option spec.

    options: JSON array like [{"attribute": "Size", "values": ["S", "M"]},
                              {"attribute": "Color", "values": ["Red", "Blue"]}]
    — defines what combinations create_variants will generate. The template
      itself is not sellable; it groups its variants.

    Returns the created template as a dict.
    """
    _require_admin()

    option_spec = _parse_json(options, "options")
    clean = []
    for group in option_spec:
        attr = str(group.get("attribute") or "").strip()
        values = [str(v).strip() for v in (group.get("values") or []) if str(v).strip()]
        if not attr:
            frappe.throw(_("Every option group needs an 'attribute' name"))
        if not values:
            frappe.throw(_("Option group '{0}' has no values").format(attr))
        # De-duplicate while preserving order.
        seen = set()
        unique_vals = [v for v in values if not (v in seen or seen.add(v))]
        clean.append({"attribute": attr, "values": unique_vals})

    if not clean:
        frappe.throw(_("A template needs at least one option group with values"))

    doc = frappe.new_doc("Product")
    doc.product_name = product_name
    doc.slug = _slugify(slug) if slug else _slugify(product_name)
    doc.status = "Active"
    doc.has_variants = 1
    doc.category = category or None
    doc.brand = brand or None
    doc.short_description = short_description or ""
    doc.description = description or ""
    doc.thumbnail = thumbnail or ""
    doc.tags = tags or ""
    doc.insert(ignore_permissions=True)

    return doc.as_dict()


@frappe.whitelist()
@handle_api_errors
def update_template(slug, status=None, thumbnail=None,
                    short_description=None, description=None, tags=None,
                    category=None, brand=None):
    """
    Update a template's metadata. Option *values* are not edited here — the
    matrix lives on the variants themselves (see create_variants); extending
    it means creating additional variants with new attribute combos.
    """
    _require_admin()
    doc = _get_product_by_slug(slug)
    if not doc.has_variants:
        frappe.throw(_("{0} is not a template product").format(slug))

    for field in ("status", "thumbnail", "short_description", "description",
                  "tags", "category", "brand"):
        val = locals().get(field)
        if val is not None:
            doc.set(field, val)

    doc.save(ignore_permissions=True)
    return doc.as_dict()


# ── Variants ──────────────────────────────────────────────────────────────────


@frappe.whitelist()
@handle_api_errors
def create_variants(template_slug, combinations, price=0,
                    status="Active", track_inventory=1):
    """
    Materialize variant Products for a template from explicit combinations.

    combinations: JSON array of attribute dicts, e.g.
        [{"Color": "Red", "Size": "L"}, {"Color": "Red", "Size": "M"}]

    Idempotent: a combination whose exact attribute set already exists on an
    active variant of this template is skipped, so re-submitting the full
    matrix after adding one option only creates the missing combos.

    Every created variant inherits the template's category/brand/media/tags
    so browse pages render it identically before per-variant assets exist.

    Returns {"created": [...], "skipped": [...], "failed": [...]}.
    """
    _require_admin()
    template = _get_product_by_slug(template_slug)
    if not template.has_variants:
        frappe.throw(_("{0} is not a template product").format(template_slug))

    combos = _parse_json(combinations, "combinations")
    if not combos:
        frappe.throw(_("combinations must contain at least one attribute dict"))

    # Index existing active variants by their normalized attribute signature.
    existing = {}
    v_rows = frappe.get_all(
        "Product",
        filters={"variant_of": template.name, "status": "Active"},
        fields=["name"],
    )
    if v_rows:
        attr_rows = frappe.get_all(
            "Product Variant Attribute",
            filters={"parent": ["in", [v.name for v in v_rows]]},
            fields=["parent", "attribute", "value"],
        )
        for r in attr_rows:
            existing.setdefault(r.parent, {})[(r.attribute or "").strip().lower()] = \
                (r.value or "").strip()

    def _signature(combo):
        return tuple(sorted((str(k).strip().lower(), str(v).strip())
                            for k, v in combo.items() if str(v).strip()))

    existing_sigs = {_signature(attrs) for attrs in existing.values()}

    created, skipped, failed = [], [], []
    for combo in combos:
        if not isinstance(combo, dict) or not combo:
            failed.append({"combination": combo, "error": "empty combination"})
            continue

        sig = _signature(combo)
        if sig in existing_sigs:
            skipped.append(dict(combo))
            continue

        # sig's keys are lowercased on purpose — signature comparison must be
        # case-insensitive. The persisted attribute name must not be: it used
        # to write straight from sig, silently lowercasing every attribute
        # name ("Color" -> "color") in the database. Original casing kept
        # here, sorted the same way sig is so display order matches.
        original_cased = {
            str(k).strip(): str(v).strip()
            for k, v in combo.items() if str(v).strip()
        }
        ordered_attrs = sorted(original_cased.items(), key=lambda kv: kv[0].lower())

        label_parts = [f"{v}" for _, v in sorted(sig)]
        try:
            doc = frappe.new_doc("Product")
            doc.product_name = f"{template.product_name} — {' / '.join(label_parts)}"
            base_slug = _slugify(f"{template.product_name} {' '.join(label_parts)}")
            candidate = base_slug
            n = 2
            while frappe.db.exists("Product", {"slug": candidate}):
                candidate = f"{base_slug}-{n}"
                n += 1
            doc.slug = candidate
            doc.status = status
            doc.variant_of = template.name
            doc.category = template.category
            doc.brand = template.brand
            doc.short_description = template.short_description
            doc.description = template.description
            doc.thumbnail = template.thumbnail
            doc.tags = template.tags
            doc.track_inventory = 1 if track_inventory else 0
            for attr, value in ordered_attrs:
                doc.append("variant_attributes", {"attribute": attr, "value": value})
            if flt(price):
                # Hub-level Retail fallback price until a vendor lists this
                # variant — get_effective_price reads these rows.
                doc.append("prices", {
                    "price_type": "Retail",
                    "price": flt(price),
                    "is_active": 1,
                })
            doc.insert(ignore_permissions=True)
            created.append({"name": doc.name, "slug": doc.slug})
            existing_sigs.add(sig)
        except Exception as e:
            failed.append({"combination": combo, "error": str(e)[:300]})

    if failed:
        frappe.log_error(
            json.dumps(failed, default=str),
            f"create_variants partial failures for {template.name}",
        )

    return {
        "template": template.name,
        "created": created,
        "skipped": skipped,
        "failed_count": len(failed),
    }


@frappe.whitelist()
@handle_api_errors
def update_variant_media(slug, thumbnail=None, media=None, is_primary=False):
    """
    Set a variant's swatch imagery. `media` is a JSON array of image URLs;
    the first becomes primary unless is_primary points elsewhere by index.
    Setting `thumbnail` alone updates just the swatch image used by option
    chips (see products._get_variant_options_map).
    """
    _require_admin()
    doc = _get_product_by_slug(slug)
    if doc.has_variants:
        frappe.throw(_("{0} is a template — set media on its variants").format(slug))

    changed = False
    if thumbnail is not None:
        doc.thumbnail = thumbnail
        changed = True

    media_list = _parse_json(media, "media")
    if media_list:
        doc.set("media", [])
        for i, url in enumerate(media_list):
            doc.append("media", {"file": url, "is_primary": 1 if (i == 0 and not is_primary) else 0})
        if not doc.thumbnail:
            doc.thumbnail = media_list[0]
        changed = True

    if changed:
        doc.save(ignore_permissions=True)
    return doc.as_dict()


@frappe.whitelist()
@handle_api_errors
def delete_variant(slug):
    """Soft-remove one variant: status → Inactive (order history keeps working)."""
    _require_admin()
    doc = _get_product_by_slug(slug)
    if doc.variant_of:
        doc.status = "Inactive"
        doc.save(ignore_permissions=True)
        return {"ok": True, "status": doc.status}
    frappe.throw(_("{0} is not a variant").format(slug))


# ── Per-item sync ─────────────────────────────────────────────────────────────

@frappe.whitelist()
@handle_api_errors
def sync_listing_to_vendor(listing_name):
    """Sync a single Vendor Listing to its vendor site.

    If the listing has a barcode and sync_enabled is checked, pushes the
    product + price + stock to the vendor's Frappe site via the event system.
    The vendor must have a frappe_site_url configured.
    """
    _require_admin()
    listing = frappe.get_doc("Vendor Listing", listing_name)

    if not listing.sync_enabled:
        frappe.throw(_("Sync is disabled for this listing"))

    if not listing.barcode:
        frappe.throw(_("Listing must have a barcode to sync"))

    vendor_url = frappe.db.get_value("Vendor", listing.vendor, "frappe_site_url")
    if not vendor_url:
        frappe.throw(_("Vendor {0} has no site URL configured").format(listing.vendor))

    # Publish product.new event to this specific vendor
    from saathimart.events.publisher import _enqueue
    product = frappe.get_doc("Product", listing.product)
    payload = {
        "product_id": product.name,
        "barcode": listing.barcode,
        "product_name": product.product_name,
        "price": listing.price,
        "stock_qty": listing.available_qty,
    }
    _enqueue("product.new", payload,
             target_site=vendor_url, target_vendor=listing.vendor,
             event_id=f"product.new.{product.name}.{listing.vendor}")

    frappe.db.set_value("Vendor Listing", listing.name, {
        "sync_status": "Synced",
        "pushed_at": frappe.utils.now_datetime(),
    }, update_modified=False)
    frappe.db.commit()

    return {"ok": True, "listing": listing.name, "vendor": listing.vendor}


@frappe.whitelist()
@handle_api_errors
def bulk_sync_listings(vendor=None, product=None):
    """Sync all eligible Vendor Listings (sync_enabled=1, barcode present)."""
    _require_admin()
    filters = {"sync_enabled": 1, "barcode": ["is", "set"], "status": "Active"}
    if vendor:
        filters["vendor"] = vendor
    if product:
        filters["product"] = product

    listings = frappe.get_all("Vendor Listing", filters=filters, pluck="name")
    synced = 0
    errors = 0
    for name in listings:
        try:
            sync_listing_to_vendor(name)
            synced += 1
        except Exception:
            errors += 1
            frappe.log_error(f"Failed to sync listing {name}", "Bulk Sync")

    return {"total": len(listings), "synced": synced, "errors": errors}
