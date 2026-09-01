"""
Product API — public catalogue endpoints with Blinkit-style filtering and sorting.

All methods are whitelisted (allow_guest=True).
"""
import json
import math

import frappe
from frappe import _
from frappe.utils import flt, nowdate, add_days, today
from saathimart.api.responses import handle_api_errors
from saathimart.api.utils import guest_rate_limit, verify_hub_secret


def _load_vendor_locations_sql(vendor_names, customer_lat, customer_lng):
    """
    Return {vendor: {vendor_name, lat, lng, service_radius_km, distance_km}}
    using MariaDB ST_Distance_Sphere. Returns ALL vendors regardless of radius;
    the caller decides whether to filter by service_radius_km.
    """
    if not vendor_names:
        return {}

    rows = frappe.db.sql("""
        SELECT name, vendor_name, lat, lng,
               COALESCE(NULLIF(service_radius_km, 0), 5) AS service_radius_km,
               ST_Distance_Sphere(
                   ST_PointFromText(CONCAT('POINT(', lng, ' ', lat, ')')),
                   ST_PointFromText(CONCAT('POINT(', %s, ' ', %s, ')'))
               ) AS distance_meters
        FROM `tabVendor`
        WHERE name IN %s
          AND lat IS NOT NULL AND lng IS NOT NULL
          AND lat != 0 AND lng != 0
        ORDER BY distance_meters ASC
    """, (customer_lng, customer_lat, tuple(vendor_names)), as_dict=True)

    result = {}
    for r in rows:
        result[r.name] = {
            "vendor_name": getattr(r, "vendor_name", r.name),
            "lat": flt(r.lat),
            "lng": flt(r.lng),
            "service_radius_km": flt(r.service_radius_km or 5),
            "distance_km": round(flt(r.distance_meters or 0) / 1000, 2),
            "has_location": True,
        }
    return result


def _preload_listing_data(product_names, customer_lat=None, customer_lng=None):
    """
    Batch-load Vendor Listing, Vendor Stock, and Vendor location data
    for a set of product names. Returns:
      listings_map: {product_name: [Vendor Listing rows]}
      stock_map: {product_name: {vendor: {available_qty, reserved_qty, physical_qty}}}
      vendor_location_map: {vendor: {vendor_name, lat, lng, service_radius_km, distance_km}}
    """
    if not product_names:
        return {}, {}, {}

    listings_map = {}
    stock_map = {}
    vendor_location_map = {}

    rows = frappe.db.sql("""
        SELECT vl.product, vl.name, vl.vendor, vl.price, vl.compare_price,
               vl.track_inventory, vl.delivery_zone, vl.estimated_delivery_minutes,
               vl.priority, vl.barcode, vl.sku, vl.vendor_product_id, vl.warehouse, vl.allow_backorder
        FROM `tabVendor Listing` vl
        WHERE vl.product IN %s AND vl.status = 'Active'
        ORDER BY vl.priority DESC, vl.price ASC
    """, (tuple(product_names),), as_dict=True)

    for r in rows:
        p = r.product
        listings_map.setdefault(p, []).append(r)

    all_vendors = set()
    for plist in listings_map.values():
        for l in plist:
            if l.vendor:
                all_vendors.add(l.vendor)

    if all_vendors:
        vs_rows = frappe.db.sql("""
            SELECT vendor, product, available_qty, reserved_qty, physical_qty
            FROM `tabVendor Stock`
            WHERE product IN %s AND vendor IN %s
        """, (tuple(product_names), tuple(all_vendors)), as_dict=True)
        for r in vs_rows:
            stock_map.setdefault(r.product, {})[r.vendor] = {
                "available_qty": flt(r.available_qty or 0),
                "reserved_qty": flt(r.reserved_qty or 0),
                "physical_qty": flt(r.physical_qty or 0),
            }

        vendor_names_rows = frappe.db.sql("""
            SELECT name, vendor_name, lat, lng, service_radius_km
            FROM `tabVendor`
            WHERE name IN %s
        """, (tuple(all_vendors),), as_dict=True)
        vendor_names_map = {r.name: r.vendor_name for r in vendor_names_rows}

        if customer_lat is not None and customer_lng is not None:
            vendor_location_map = _load_vendor_locations_sql(
                list(all_vendors), customer_lat, customer_lng
            )

        for vendor_id, vname in vendor_names_map.items():
            if vendor_id not in vendor_location_map:
                vendor_location_map[vendor_id] = {
                    "vendor_name": vname,
                    "lat": 0,
                    "lng": 0,
                    "service_radius_km": 0,
                    "distance_km": 9999,
                    "has_location": False,
                }
            else:
                vendor_location_map[vendor_id]["has_location"] = True

    return listings_map, stock_map, vendor_location_map


def _get_best_vendor_listing(product_name, vendor=None, delivery_zone=None,
                             customer_lat=None, customer_lng=None,
                             _listings_map=None, _stock_map=None, _vendor_location_map=None):
    """Return the best Vendor Listing for a product using pre-loaded data."""
    vendor_location_map = _vendor_location_map or {}
    if _listings_map is not None:
        listings = _listings_map.get(product_name, [])
    else:
        cache_key = (
            f"sm_best_listing:{product_name}:{vendor or ''}:"
            f"{delivery_zone or ''}:{customer_lat or ''}:{customer_lng or ''}"
        )
        cached = frappe.cache().get_value(cache_key)
        if cached:
            return cached
        listings = frappe.get_list(
            "Vendor Listing",
            filters={"product": product_name, "status": "Active"},
            fields=["name", "vendor", "price", "compare_price",
                    "track_inventory", "delivery_zone", "estimated_delivery_minutes",
                    "priority", "sku", "vendor_product_id", "warehouse", "allow_backorder"],
            order_by="priority desc, price asc",
        )
        if listings:
            vendor_names = [l.vendor for l in listings if l.vendor]
            if customer_lat is not None and customer_lng is not None and vendor_names:
                vendor_location_map = _load_vendor_locations_sql(
                    vendor_names, customer_lat, customer_lng
                )

    # Normalize stock_map: always {vendor: stock_data}
    if _stock_map is not None and product_name in _stock_map:
        stock_map = _stock_map[product_name]
    elif _stock_map is None:
        stock_map = {}
        vendor_names = [l.vendor for l in listings if l.vendor]
        if vendor_names:
            rows = frappe.db.sql("""
                SELECT vendor, available_qty, reserved_qty, physical_qty
                FROM `tabVendor Stock`
                WHERE product = %s AND vendor IN %s
            """, (product_name, tuple(vendor_names)), as_dict=True)
            stock_map = {r.vendor: r for r in rows}
    else:
        stock_map = {}

    if not listings:
        return None

    def _enrich(listing):
        s = stock_map.get(listing.vendor, {})
        listing.available_qty = s.get("available_qty", 0)
        listing.reserved_qty = s.get("reserved_qty", 0)
        listing.physical_qty = s.get("physical_qty", 0)
        loc = vendor_location_map.get(listing.vendor)
        if loc:
            listing.vendor_name = loc.get("vendor_name") or listing.vendor_name or ""
            listing.vendor_lat = loc.get("lat", 0)
            listing.vendor_lng = loc.get("lng", 0)
            listing.vendor_service_radius_km = loc.get("service_radius_km", 0)
            listing.distance_km = loc.get("distance_km", 0)
            listing.has_location = bool(loc.get("has_location"))
        return listing

    if vendor:
        candidates = [l for l in listings if l.vendor == vendor]
        if delivery_zone:
            zoned = [l for l in candidates if l.delivery_zone == delivery_zone]
            if zoned:
                result = _enrich(zoned[0])
                if _listings_map is None:
                    frappe.cache().set_value(cache_key, result, expires_in_sec=300)
                return result
        base = [l for l in candidates if not l.delivery_zone]
        if base:
            result = _enrich(base[0])
            if _listings_map is None:
                frappe.cache().set_value(cache_key, result, expires_in_sec=300)
            return result
        if candidates:
            result = _enrich(candidates[0])
            if _listings_map is None:
                frappe.cache().set_value(cache_key, result, expires_in_sec=300)
            return result

    if delivery_zone:
        zone_listings = [l for l in listings if l.delivery_zone == delivery_zone]
        if zone_listings:
            in_stock = [l for l in zone_listings
                        if not l.track_inventory or flt(stock_map.get(l.vendor, {}).get("available_qty") or 0) > 0]
            vl = (in_stock or zone_listings)[0]
            result = _enrich(vl)
            if _listings_map is None:
                frappe.cache().set_value(cache_key, result, expires_in_sec=300)
            return result

    if vendor_location_map:
        # Sort vendors: prefer in-stock within radius, then out-of-stock within radius,
        # then in-stock outside radius, then out-of-stock outside radius.
        # This keeps the existing approach of deprioritising but not
        # hard-excluding out-of-stock vendors.
        in_stock_within = []   # Within radius, in stock
        out_of_stock_within = []  # Within radius, out of stock (deprioritised)
        in_stock_outside = []  # Outside radius, in stock
        out_of_stock_outside = []  # Outside radius, out of stock (last resort)

        for l in listings:
            loc = vendor_location_map.get(l.vendor)
            if not loc:
                continue
            if loc.get("has_location") is False:
                continue

            enriched = _enrich(l)
            is_in_stock = not l.track_inventory or flt(stock_map.get(l.vendor, {}).get("available_qty") or 0) > 0
            within_radius = loc["distance_km"] <= loc["service_radius_km"]

            if within_radius and is_in_stock:
                in_stock_within.append(enriched)
            elif within_radius and not is_in_stock:
                out_of_stock_within.append(enriched)
            elif not within_radius and is_in_stock:
                in_stock_outside.append(enriched)
            else:
                out_of_stock_outside.append(enriched)

        # Return best candidate from each tier, sorted by distance
        for tier in (in_stock_within, in_stock_outside, out_of_stock_within, out_of_stock_outside):
            if tier:
                tier.sort(key=lambda x: x.distance_km)
                result = tier[0]
                if _listings_map is None:
                    frappe.cache().set_value(cache_key, result, expires_in_sec=300)
                return result

    mode = frappe.db.get_single_value("Settings", "vendor_selection_mode") or "Highest Priority"
    if mode == "Lowest Price":
        listings.sort(key=lambda l: flt(l.price))
    elif mode == "Lowest Delivery Time":
        listings.sort(key=lambda l: flt(l.estimated_delivery_minutes))
    elif mode == "Nearest" and vendor_location_map:
        listings_with_loc = [_enrich(l) for l in listings if l.vendor in vendor_location_map]
        if listings_with_loc:
            listings_with_loc.sort(key=lambda l: flt(getattr(l, "distance_km", 9999)))
            result = listings_with_loc[0]
            if _listings_map is None:
                frappe.cache().set_value(cache_key, result, expires_in_sec=300)
            return result
    else:
        listings.sort(key=lambda l: flt(l.priority), reverse=True)

    result = _enrich(listings[0])
    if _listings_map is None:
        frappe.cache().set_value(cache_key, result, expires_in_sec=300)
    return result


def _get_best_template_listing(template_name, vendor=None, delivery_zone=None,
                               customer_lat=None, customer_lng=None):
    """
    A has_variants=1 template has no Vendor Listings of its own — its price/
    stock on a browse card or product page is the cheapest active listing
    across its own variants (in-stock ones preferred), the same "starting
    from ₹X" summary any variant-based storefront shows before a customer
    has picked a specific size/color. Returns a listing row shaped exactly
    like _get_best_vendor_listing's return value so callers don't need to
    know which one they got.
    """
    variant_names = frappe.get_list(
        "Product", filters={"variant_of": template_name, "status": "Active"},
        pluck="name",
    )
    if not variant_names:
        return None

    listings_map, stock_map, vendor_location_map = _preload_listing_data(
        variant_names, customer_lat=customer_lat, customer_lng=customer_lng
    )
    candidates = []
    for vn in variant_names:
        best = _get_best_vendor_listing(
            vn, vendor=vendor, delivery_zone=delivery_zone,
            customer_lat=customer_lat, customer_lng=customer_lng,
            _listings_map=listings_map, _stock_map=stock_map,
            _vendor_location_map=vendor_location_map,
        )
        if best:
            candidates.append(best)
    if not candidates:
        return None

    in_stock = [c for c in candidates
                if not c.track_inventory or flt(getattr(c, "available_qty", 0) or 0) > 0]
    pool = in_stock or candidates
    return min(pool, key=lambda c: flt(c.price))


def _resolve_best_listing(doc_or_name, has_variants, vendor=None, delivery_zone=None,
                          customer_lat=None, customer_lng=None,
                          _listings_map=None, _stock_map=None, _vendor_location_map=None):
    """Single entry point for "what listing represents this product" —
    branches to the template-aggregate resolver when has_variants is set,
    otherwise the normal per-product resolver. See both functions' own
    docstrings."""
    name = doc_or_name if isinstance(doc_or_name, str) else doc_or_name.name
    if has_variants:
        return _get_best_template_listing(
            name, vendor=vendor, delivery_zone=delivery_zone,
            customer_lat=customer_lat, customer_lng=customer_lng,
        )
    return _get_best_vendor_listing(
        name, vendor=vendor, delivery_zone=delivery_zone,
        customer_lat=customer_lat, customer_lng=customer_lng,
        _listings_map=_listings_map, _stock_map=_stock_map,
        _vendor_location_map=_vendor_location_map,
    )


def _get_variant_summaries(template_name, vendor=None, delivery_zone=None,
                           customer_lat=None, customer_lng=None, exclude=None):
    """Serialized list of a template's active variants (each variant's own
    price/stock/attributes) — what a frontend renders as the size/color
    picker on a product page."""
    filters = {"status": "Active", "variant_of": template_name}
    if exclude:
        filters["name"] = ["!=", exclude]
    variant_names = frappe.get_list(
        "Product", filters=filters, pluck="name", order_by="creation asc"
    )
    if not variant_names:
        return []

    listings_map, stock_map, vendor_location_map = _preload_listing_data(
        variant_names, customer_lat=customer_lat, customer_lng=customer_lng
    )
    return [
        _serialize_product(
            frappe.get_doc("Product", vn), _listings_map=listings_map,
            _stock_map=stock_map, _vendor_location_map=vendor_location_map,
            vendor=vendor, delivery_zone=delivery_zone,
            customer_lat=customer_lat, customer_lng=customer_lng,
        )
        for vn in variant_names
    ]


def _get_variant_options_map(template_names):
    """
    {template_name: {"variant_count": n, "options": [{attribute, values}]}}
    for every template in `template_names` — what the storefront renders as
    the size/color chips on a browse card or above the picker on a product
    page ("Size: S / M / L · Color: Red / Blue"), without loading each
    variant's full listing data.

    Each option value also carries a `swatch` — the first variant with that
    value whose own thumbnail is set — so color pickers render real image
    swatches instead of plain text chips. Values with no variant imagery
    get swatch=None; the frontend falls back to text chips.

    Two flat queries total regardless of template count — never per-template
    lookups inside a page loop.
    """
    if not template_names:
        return {}

    variant_rows = frappe.get_all(
        "Product",
        filters={"variant_of": ["in", template_names], "status": "Active"},
        fields=["name", "variant_of", "thumbnail"],
        order_by="creation asc",
    )
    count_map = {}
    for v in variant_rows:
        count_map[v.variant_of] = count_map.get(v.variant_of, 0) + 1

    options_by_template = {}
    attr_rows_by_variant = {}
    if variant_rows:
        attr_rows = frappe.get_all(
            "Product Variant Attribute",
            filters={"parent": ["in", [v.name for v in variant_rows]]},
            fields=["parent", "attribute", "value"],
            order_by="idx asc",
        )
        variant_to_template = {v.name: v.variant_of for v in variant_rows}
        attr_rows_by_variant = {}
        for r in attr_rows:
            attr_rows_by_variant.setdefault(r.parent, []).append(r)
            tmpl = variant_to_template.get(r.parent)
            if not tmpl or not r.attribute:
                continue
            values = options_by_template.setdefault(tmpl, {}).setdefault(r.attribute, [])
            if r.value not in values:
                values.append(r.value)

    # Swatch resolution: first variant (creation order) carrying each value
    # AND a non-empty thumbnail wins. Variant rows are creation-ordered, so
    # iterating once in order gives deterministic winners.
    swatch_by_key = {}  # (template, attribute_lower, value) -> thumbnail
    seen_keys = set()
    for v in variant_rows:
        if not (getattr(v, "thumbnail", None) or "").strip():
            continue
        for r in attr_rows_by_variant.get(v.name, []):
            key = (v.variant_of, (r.attribute or "").strip().lower(), (r.value or "").strip())
            if key not in seen_keys:
                seen_keys.add(key)
                swatch_by_key[key] = v.thumbnail

    result = {}
    for t in template_names:
        options = []
        for attr, vals in options_by_template.get(t, {}).items():
            options.append({
                "attribute": attr,
                "values": [
                    {
                        "value": val,
                        "swatch": swatch_by_key.get((t, attr.strip().lower(), val)),
                    }
                    for val in vals
                ],
            })
        result[t] = {"variant_count": count_map.get(t, 0), "options": options}
    return result


def _get_product_images(product_name):
    """
    Return image metadata with lazy-loading placeholders for each product image.

    Each image includes:
      - url:            full image URL
      - thumbnail:      64px blurred placeholder (data URI or URL)
      - width, height:  intrinsic dimensions (or defaults)
      - is_primary:     whether this is the hero image

    The thumbnail field is a low-resolution placeholder that the frontend
    displays while the full image loads. For Frappe-hosted images, we
    generate a small inline blurred version. For external URLs (CDN/S3),
    we return a separate thumbnail URL if one exists.

    This replaces the need for client-side blurhash decoding — the server
    provides ready-to-display placeholders.
    """
    media_rows = frappe.get_all(
        "Product Media",
        filters={"parent": product_name},
        fields=["file", "is_primary", "alt_text"],
        order_by="is_primary desc, idx asc",
    )

    images = []
    for m in media_rows:
        url = m.get("file") or ""
        if not url:
            continue

        # Generate thumbnail URL by appending ?w=64 to request server-side resize
        # or use Frappe's built-in thumbnail generation
        thumb_url = ""
        if "/files/" in url:
            # Frappe file — append thumbnail parameter
            thumb_url = f"{url}?w=64&q=10"
        elif url.startswith("http"):
            # External CDN — append width param if no query string
            separator = "&" if "?" in url else "?"
            thumb_url = f"{url}{separator}w=64&q=10"

        images.append({
            "url": url,
            "thumbnail": thumb_url,
            "width": 600,   # default; frontend should measure on load
            "height": 600,
            "is_primary": bool(m.get("is_primary")),
            "alt_text": m.get("alt_text") or product_name,
        })

    # Ensure there's at least one entry (from product.thumbnail if no media)
    if not images:
        thumbnail = frappe.db.get_value("Product", product_name, "thumbnail")
        if thumbnail:
            thumb_url = f"{thumbnail}?w=64&q=10" if "/files/" in thumbnail else thumbnail
            images.append({
                "url": thumbnail,
                "thumbnail": thumb_url,
                "width": 600,
                "height": 600,
                "is_primary": True,
                "alt_text": product_name,
            })

    return images


def _serialize_product(doc, _listings_map=None, _stock_map=None, _vendor_location_map=None,
                       vendor=None, delivery_zone=None, customer_lat=None, customer_lng=None):
    """Serialize Product doc with its best vendor listing data."""
    primary_media = ""
    media_files = []
    for m in (getattr(doc, "media", None) or []):
        if m.file:
            media_files.append(m.file)
            if m.is_primary:
                primary_media = m.file
    if not primary_media and media_files:
        primary_media = media_files[0]

    best_listing = _resolve_best_listing(
        doc, getattr(doc, "has_variants", 0), vendor=vendor, delivery_zone=delivery_zone,
        customer_lat=customer_lat, customer_lng=customer_lng,
        _listings_map=_listings_map, _stock_map=_stock_map,
        _vendor_location_map=_vendor_location_map,
    )
    price = flt(best_listing.price) if best_listing else 0
    compare = flt(best_listing.compare_price) if best_listing and best_listing.compare_price else 0
    discount = 0
    if compare > price:
        discount = round(((compare - price) / compare) * 100, 1)

    stock_qty = flt(best_listing.available_qty) if best_listing else 0
    track_inventory = best_listing.track_inventory if best_listing else 1
    allow_backorder = best_listing.allow_backorder if best_listing else 0
    vendor = best_listing.vendor if best_listing else None
    sku = best_listing.sku if best_listing else ""
    vendor_product_id = best_listing.vendor_product_id if best_listing else ""
    barcode = best_listing.barcode if best_listing else ""
    delivery_zone = best_listing.delivery_zone if best_listing else ""

    variant_attributes = [
        {"attribute": r.attribute, "value": r.value}
        for r in (getattr(doc, "variant_attributes", None) or [])
    ]

    return {
        "name": doc.name,
        "product_name": doc.product_name,
        "slug": doc.slug,
        "price": price,
        "compare_price": compare,
        "thumbnail": primary_media or doc.thumbnail,
        "stock_qty": stock_qty,
        "track_inventory": track_inventory,
        "allow_backorder": allow_backorder,
        "category": doc.category,
        "vendor": vendor,
        "vendor_name": getattr(best_listing, "vendor_name", "") or "",
        "vendor_lat": flt(getattr(best_listing, "vendor_lat", 0) or 0),
        "vendor_lng": flt(getattr(best_listing, "vendor_lng", 0) or 0),
        "vendor_service_radius_km": flt(getattr(best_listing, "vendor_service_radius_km", 0) or 0),
        "distance_km": flt(getattr(best_listing, "distance_km", 0) or 0),
        "short_description": doc.short_description or "",
        "is_on_sale": compare > price,
        "discount_pct": discount,
        "tags": getattr(doc, "tags", "") or "",
        "sku": sku,
        "barcode": barcode,
        "vendor_product_id": vendor_product_id,
        "delivery_zone": delivery_zone,
        "media": media_files,
        "avg_rating": flt(doc.avg_rating or 0),
        "review_count": doc.review_count or 0,
        "brand": getattr(doc, "brand", "") or "",
        "has_variants": getattr(doc, "has_variants", 0) or 0,
        "variant_of": getattr(doc, "variant_of", "") or "",
        "variant_attributes": variant_attributes,
        # Lazy-loading: thumbnail placeholder URL (64px blurred preview)
        "image_thumbnail": f"{primary_media or doc.thumbnail}?w=64&q=10" if (primary_media or doc.thumbnail) else "",
    }


@frappe.whitelist(allow_guest=True)
def list_products(category=None, vendor=None, search=None, page=1, page_size=20,
                  sort=None, in_stock=None, min_price=None, max_price=None, tags=None,
                  brand=None, delivery_zone=None, lat=None, lng=None, radius_km=5):
    """
    Blinkit-style product listing with rich filters and sorting.

    Sort options:
      price_asc, price_desc, newest, popularity, rating

    Location params:
      lat, lng — customer coordinates; when provided, vendors are sorted by distance
      radius_km — maximum distance in km for a vendor to be considered (default: 5)
    """
    guest_rate_limit("products.list", limit=300, window_seconds=60)
    # Resolve location from params or cart fallback (matching saathi_middleware)
    from saathimart.api.cart import _get_customer_location
    lat, lng = _get_customer_location(None, lat, lng)
    # Product has no is_active field of its own (only Category and the
    # Product Price child row do) — filtering on it here made Frappe
    # silently resolve it against the unrelated Product Price child table
    # and LEFT JOIN it in, duplicating rows per matching price row or
    # dropping products with no active price row at all. status covers
    # "is this product active" on its own, matching every other query below.
    #
    # variant_of "is not set" keeps individual variants (e.g. "T-Shirt —
    # Red, L") out of the browse grid — only the template ("T-Shirt", with
    # has_variants=1) or a standalone non-variant product shows here.
    # Customers pick a specific variant on the template's own product page
    # (see get_product's `variants` list), so the grid doesn't end up with
    # one card per size/color of the same item.
    filters = {"status": "Active", "variant_of": ["is", "not set"]}

    # Category filter — accept a single slug/name or a comma-separated list
    if category:
        cat_names = []
        for token in str(category).split(","):
            token = token.strip()
            if not token:
                continue
            cat_name = frappe.db.get_value("Category", {"slug": token}, "name")
            if not cat_name:
                cat_name = frappe.db.get_value("Category", {"category_name": token}, "name")
            if cat_name:
                cat_names.append(cat_name)
        if cat_names:
            filters["category"] = ["in", cat_names]

    # Brand filter — accept a single slug/name or a comma-separated list
    # (same resolution style as the Category filter above). Unmatched brand
    # tokens are ignored rather than returning nothing, so "nestle,typo"
    # still filters by nestle instead of blanking the grid.
    if brand:
        brand_names = []
        for token in str(brand).split(","):
            token = token.strip()
            if not token:
                continue
            b_name = frappe.db.get_value("Brand", {"slug": token}, "name")
            if not b_name:
                b_name = frappe.db.get_value("Brand", {"brand_name": token}, "name")
            if b_name and b_name not in brand_names:
                brand_names.append(b_name)
        if brand_names:
            filters["brand"] = ["in", brand_names]

    # Search filter
    or_filters = None
    if search:
        or_filters = [
            ["product_name", "like", f"%{search}%"],
            ["tags", "like", f"%{search}%"],
            ["short_description", "like", f"%{search}%"],
        ]

    page = max(1, int(page))
    page_size = min(100, max(1, int(page_size)))

    # Build order_by
    order_by = "creation desc"
    if sort == "price_asc":
        order_by = "creation desc"
    elif sort == "price_desc":
        order_by = "creation desc"
    elif sort == "newest":
        order_by = "creation desc"
    elif sort == "popularity":
        order_by = "modified desc"

    products = frappe.get_list(
        "Product",
        filters=filters,
        or_filters=or_filters,
        fields=["name", "product_name", "slug", "category", "status",
                "short_description", "tags", "thumbnail", "avg_rating",
                "review_count", "brand", "has_variants"],
        limit_start=(page - 1) * page_size,
        limit_page_length=page_size,
        order_by=order_by,
    )

    # Batch-load all listing data for this page (eliminates N+1)
    product_names = [p["name"] for p in products]
    clat = flt(lat) if lat is not None else None
    clng = flt(lng) if lng is not None else None
    listings_map, stock_map, vendor_location_map = _preload_listing_data(
        product_names, customer_lat=clat, customer_lng=clng
    )

    max_radius = flt(radius_km) if radius_km is not None else None

    # Apply price/stock/vendor filtering in Python (vendor listing aware)
    filtered = []
    for p in products:
        # A has_variants template has no Vendor Listing of its own — its
        # card price/stock is aggregated across its variants instead (see
        # _resolve_best_listing); a plain product resolves the normal way.
        best = _resolve_best_listing(
            p["name"], p.get("has_variants"), vendor=vendor, delivery_zone=delivery_zone,
            customer_lat=clat, customer_lng=clng,
            _listings_map=listings_map, _stock_map=stock_map,
            _vendor_location_map=vendor_location_map,
        )
        if not best:
            continue

        price = flt(best.price)
        compare = flt(best.compare_price or 0)
        stock = flt(best.available_qty)

        # Price range filter
        if min_price is not None and price < flt(min_price):
            continue
        if max_price is not None and price > flt(max_price):
            continue

        # In-stock filter
        if in_stock and str(in_stock) in ("1", "true", "True"):
            if best.track_inventory and stock <= 0:
                continue

        # Tags filter
        if tags:
            tag_list = [t.strip() for t in str(tags).split(",") if t.strip()]
            product_tags = (p.get("tags") or "").lower()
            if not any(tag.lower() in product_tags for tag in tag_list):
                continue

        # Distance and availability check. The Vendor document is the source
        # of truth for service_radius_km; radius_km is only the request-level
        # maximum search distance. A location-aware catalogue must not return
        # an item that the selected vendor cannot deliver.
        distance = flt(getattr(best, "distance_km", 0) or 0)
        has_location = getattr(best, "has_location", False)
        service_radius = flt(getattr(best, "vendor_service_radius_km", 0) or 0)
        location_aware = clat is not None and clng is not None
        outside_radius = bool(
            location_aware
            and max_radius is not None
            and (
                not has_location
                or distance > max_radius
                or (service_radius > 0 and distance > service_radius)
            )
        )
        if outside_radius:
            continue

        p["price"] = price
        p["compare_price"] = compare
        p["stock_qty"] = stock
        p["track_inventory"] = best.track_inventory
        p["vendor"] = best.vendor
        p["vendor_name"] = getattr(best, "vendor_name", "") or ""
        p["vendor_lat"] = flt(getattr(best, "vendor_lat", 0) or 0)
        p["vendor_lng"] = flt(getattr(best, "vendor_lng", 0) or 0)
        p["vendor_service_radius_km"] = flt(getattr(best, "vendor_service_radius_km", 0) or 0)
        p["distance_km"] = distance
        p["sku"] = best.sku or ""
        p["barcode"] = best.barcode or ""
        p["vendor_product_id"] = best.vendor_product_id or ""
        p["delivery_zone"] = best.delivery_zone or ""
        p["outside_radius"] = outside_radius
        filtered.append(p)

    # Sort by distance if location provided
    if lat is not None and lng is not None:
        filtered.sort(key=lambda x: x.get("distance_km", 9999))
    # Sort by price if requested
    elif sort == "price_asc":
        filtered.sort(key=lambda x: x.get("price", 0))
    elif sort == "price_desc":
        filtered.sort(key=lambda x: x.get("price", 0), reverse=True)
    elif sort == "rating":
        filtered.sort(key=lambda x: x.get("avg_rating", 0), reverse=True)

    serialized = [_serialize_product(p, vendor=vendor, delivery_zone=delivery_zone,
                                     customer_lat=clat, customer_lng=clng) for p in filtered]

    # Variant metadata for grid cards — templates show their option chips
    # ("Size: S / M / L") and variant count without loading full variant
    # listing data. One batched lookup for the whole page.
    options_map = _get_variant_options_map([p["name"] for p in filtered if p.get("has_variants")])
    for card in serialized:
        meta = options_map.get(card.get("name"))
        if meta:
            card["variant_count"] = meta["variant_count"]
            card["options"] = meta["options"]

    cache_key = (
        f"sm_list_products:{category or ''}:{vendor or ''}:{search or ''}:"
        f"{page}:{page_size}:{sort or ''}:{lat or ''}:{lng or ''}:"
        f"{delivery_zone or ''}:{min_price or ''}:{max_price or ''}:"
        f"{in_stock or ''}:{tags or ''}:{radius_km or ''}:{brand or ''}"
    )
    frappe.cache().set_value(cache_key, {
        "items": serialized,
        "page": page,
        "page_size": page_size,
        "total": len(serialized),
    }, expires_in_sec=60)

    return {
        "items": serialized,
        "page": page,
        "page_size": page_size,
        "total": len(serialized),
    }


@frappe.whitelist(allow_guest=True)
def get_product(slug, vendor=None, delivery_zone=None, lat=None, lng=None, radius_km=5):
    """Full product detail with vendor-specific pricing and location."""
    guest_rate_limit("products.get", limit=300, window_seconds=60)
    # Resolve location from params or cart fallback (matching saathi_middleware)
    from saathimart.api.cart import _get_customer_location
    lat, lng = _get_customer_location(None, lat, lng)
    name = frappe.db.get_value("Product", {"slug": slug, "status": "Active"}, "name")
    if not name:
        frappe.throw(_("Product not found"), frappe.DoesNotExistError)

    # ── ETag support: return 304 if client already has current version ──
    import hashlib
    etag_source = f"{name}:{vendor or ''}:{delivery_zone or ''}:{lat or ''}:{lng or ''}:{radius_km or ''}"
    etag = hashlib.md5(etag_source.encode()).hexdigest()
    # frappe.request is unbound outside a real HTTP request (bench execute,
    # tests, background jobs) — LocalProxy raises RuntimeError there.
    if_modified_since = None
    if frappe.request:
        if_modified_since = (
            frappe.request.headers.get("If-None-Match")
            or frappe.request.headers.get("If-Modified-Since")
        )
    if if_modified_since and if_modified_since.strip('"') == etag:
        from frappe import response as frappe_response
        frappe_response.status_code = 304
        frappe_response.body = b""
        frappe.local.response = frappe_response
        raise Exception("304 Not Modified")

    cache_key = f"sm_product:{name}:{vendor or delivery_zone or 'hub'}:{lat or ''}:{lng or ''}:{radius_km or ''}"
    cached = frappe.cache().get_value(cache_key)
    if cached:
        cached["_etag"] = etag
        return cached

    doc = frappe.get_doc("Product", name)
    clat = flt(lat) if lat is not None else None
    clng = flt(lng) if lng is not None else None
    max_radius = flt(radius_km) if radius_km is not None else None

    listings_map, stock_map, vendor_location_map = _preload_listing_data(
        [name], customer_lat=clat, customer_lng=clng
    )

    # Check if the selected vendor is within its configured service radius
    # before serializing. Skipped
    # for a has_variants template — it has no listings/location of its own
    # to check; availability is a per-variant question, reflected in each
    # variant's own price/stock once the customer picks one below.
    # Instead of throwing an error when no vendor is within radius, keep the
    # product detail available and expose a flag for the frontend to explain
    # that delivery is unavailable at the selected location.
    outside_radius = False
    if clat is not None and clng is not None and max_radius is not None and not doc.has_variants:
        best_check = _get_best_vendor_listing(
            name, vendor=vendor, delivery_zone=delivery_zone,
            customer_lat=clat, customer_lng=clng,
            _listings_map=listings_map, _stock_map=stock_map,
            _vendor_location_map=vendor_location_map,
        )
        distance = flt(getattr(best_check, "distance_km", 0) or 0) if best_check else 0
        service_radius = flt(getattr(best_check, "vendor_service_radius_km", 0) or 0) if best_check else 0
        if (
            not best_check
            or not getattr(best_check, "has_location", False)
            or distance > max_radius
            or (service_radius > 0 and distance > service_radius)
        ):
            outside_radius = True

    data = _serialize_product(doc, _listings_map=listings_map, _stock_map=stock_map,
                              _vendor_location_map=vendor_location_map,
                              vendor=vendor, delivery_zone=delivery_zone,
                              customer_lat=clat, customer_lng=clng)
    data["vendor_context"] = vendor or data.get("vendor")
    data["outside_radius"] = outside_radius

    # Add all vendor listings
    listings = frappe.get_list(
        "Vendor Listing",
        filters={"product": name},
        fields=["name", "vendor", "price", "compare_price", "available_qty",
                "reserved_qty", "track_inventory", "allow_backorder",
                "delivery_zone", "estimated_delivery_minutes", "priority",
                "sku", "vendor_product_id", "warehouse", "status",
                "last_updated", "last_sync_at"],
        order_by="priority desc, price asc",
    )

    # Enrich with vendor location if customer location provided
    if clat is not None and clng is not None:
        for l in listings:
            loc = vendor_location_map.get(l.vendor)
            if loc and loc.get("has_location"):
                l.vendor_name = loc["vendor_name"]
                l.vendor_lat = loc["lat"]
                l.vendor_lng = loc["lng"]
                l.vendor_service_radius_km = loc["service_radius_km"]
                l.distance_km = loc["distance_km"]
            elif loc:
                l.vendor_name = loc["vendor_name"]

        listings.sort(key=lambda x: x.get("distance_km", 9999))
    else:
        for l in listings:
            loc = vendor_location_map.get(l.vendor)
            if loc:
                l.vendor_name = loc["vendor_name"]

    data["vendor_listings"] = listings

    # Variant switcher data: a template's own children, or a variant's
    # siblings (+ a pointer back to its template) — same shape either way
    # so the frontend doesn't need to special-case which slug it loaded.
    if doc.has_variants:
        data["variants"] = _get_variant_summaries(
            name, vendor=vendor, delivery_zone=delivery_zone,
            customer_lat=clat, customer_lng=clng,
        )
        data["variant_of_product"] = None
        # Option groups for the picker UI — ordered, de-duplicated values
        # across this template's active variants.
        data.update(_get_variant_options_map([name])[name])
    elif doc.variant_of:
        data["variants"] = _get_variant_summaries(
            doc.variant_of, vendor=vendor, delivery_zone=delivery_zone,
            customer_lat=clat, customer_lng=clng, exclude=name,
        )
        data["variant_of_product"] = frappe.db.get_value(
            "Product", doc.variant_of, ["name", "product_name", "slug"], as_dict=True
        )
    else:
        data["variants"] = []
        data["variant_of_product"] = None

    # Add review summary
    review_stats = frappe.db.sql("""
        SELECT COUNT(*) as review_count, AVG(rating) as avg_rating
        FROM `tabReview`
        WHERE product = %s AND status = 'Approved'
    """, (name,), as_dict=True)
    if review_stats:
        data["review_count"] = review_stats[0].review_count or 0
        data["avg_rating"] = round(flt(review_stats[0].avg_rating or 0), 1)
    else:
        data["review_count"] = 0
        data["avg_rating"] = 0

    # Add related products from same category
    import random

    related_pool = frappe.get_list(
        "Product",
        filters={"status": "Active", "category": doc.category, "name": ["!=", name],
                 "variant_of": ["is", "not set"]},
        fields=["name", "product_name", "slug", "category"],
        order_by="creation desc",
        limit_page_length=32,
    )
    related = random.sample(related_pool, min(8, len(related_pool)))
    data["related_products"] = [_serialize_product(frappe.get_doc("Product", p["name"])) for p in related]

    # ── BlurHash / lazy-loading image metadata ──
    data["images"] = _get_product_images(name)
    data["_etag"] = etag
    frappe.cache().set_value(cache_key, data, expires_in_sec=300)
    return data


@frappe.whitelist(allow_guest=True)
def list_categories():
    """Return all active categories, optionally filtered by parent."""
    return frappe.get_list(
        "Category",
        filters={"is_active": 1},
        fields=["name", "category_name", "slug", "image", "parent_category", "sort_order"],
        order_by="sort_order asc, category_name asc",
    )


@frappe.whitelist(allow_guest=True)
def list_brands():
    """All active brands with item counts for the filter sidebar."""
    cache_key = "sm_brands_list"
    cached = frappe.cache().get_value(cache_key)
    if cached:
        return cached

    brands = frappe.get_list(
        "Brand",
        filters={"is_active": 1},
        fields=["name", "brand_name", "slug", "logo", "sort_order"],
        order_by="sort_order asc",
    )

    for b in brands:
        b["count"] = frappe.db.count(
            "Product",
            {"brand": b["name"], "status": "Active"},
        )
    # Filter out brands with zero items
    brands = [b for b in brands if b["count"] > 0]

    frappe.cache().set_value(cache_key, brands, expires_in_sec=300)
    return brands


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def get_variant(slug, attributes=None, vendor=None, delivery_zone=None,
                lat=None, lng=None, radius_km=5):
    """
    Resolve ONE sellable variant of a template product by its attribute
    combination — the endpoint behind picking "Red / Large" on the
    storefront's variant picker.

    `slug` accepts the template's slug OR any sibling variant's slug (so a
    customer landing on a variant URL can switch options without tracking
    the template id). `attributes` is a JSON object like
    '{"Color": "Red", "Size": "Large"}'; attribute names match
    case-insensitively, values exactly. Omitted attributes are free — the
    first active variant matching everything supplied wins, so a picker can
    resolve progressively as the customer chooses.

    Returns the same full payload as get_product for the matched variant:
    its own price/stock/vendor listings plus its siblings for the switcher.
    """
    guest_rate_limit("products.variant", limit=300, window_seconds=60)

    name = frappe.db.get_value("Product", {"slug": slug, "status": "Active"}, "name")
    if not name:
        frappe.throw(_("Product not found"), frappe.DoesNotExistError)

    doc = frappe.get_doc("Product", name)
    template_name = doc.name if doc.has_variants else doc.variant_of
    if not template_name:
        frappe.throw(_("Product {0} has no variants").format(slug))

    if isinstance(attributes, str):
        try:
            attributes = json.loads(attributes)
        except (TypeError, ValueError):
            frappe.throw(_("attributes must be a valid JSON object"))
    wanted_raw = dict(attributes or {})
    wanted = {
        str(k).strip().lower(): str(v).strip()
        for k, v in wanted_raw.items() if str(v).strip()
    }

    variants = frappe.get_all(
        "Product",
        filters={"variant_of": template_name, "status": "Active"},
        fields=["name", "slug"],
        order_by="creation asc",
    )
    if not variants:
        frappe.throw(_("No variants available for this product"), frappe.DoesNotExistError)

    attr_rows = frappe.get_all(
        "Product Variant Attribute",
        filters={"parent": ["in", [v.name for v in variants]]},
        fields=["parent", "attribute", "value"],
    )
    attrs_by_variant = {}
    for r in attr_rows:
        attrs_by_variant.setdefault(r.parent, {})[(r.attribute or "").strip().lower()] = (r.value or "").strip()

    if wanted:
        candidates = [
            v.slug for v in variants
            if all(attrs_by_variant.get(v.name, {}).get(k) == val for k, val in wanted.items())
        ]
    else:
        # No constraints given — deterministic default: oldest active variant.
        candidates = [v.slug for v in variants]

    if not candidates:
        frappe.throw(
            _("No variant matches the selected options"), frappe.DoesNotExistError
        )

    # Full product-detail payload — the storefront gets the matched
    # variant's price/stock/listings AND its siblings in one round trip.
    return get_product(candidates[0], vendor=vendor, delivery_zone=delivery_zone,
                       lat=lat, lng=lng, radius_km=radius_km)


@frappe.whitelist(allow_guest=True)
def get_brands():
    """
    All active brands with active-product counts, for the storefront's
    brand filter sidebar.
    Brands with zero products are hidden.
    """
    guest_rate_limit("products.brands", limit=300, window_seconds=60)
    cache_key = "sm_brands_list"
    cached = frappe.cache().get_value(cache_key)
    if cached is not None:
        return cached

    brands = frappe.get_list(
        "Brand",
        filters={"is_active": 1},
        fields=["name", "brand_name", "slug", "logo", "sort_order"],
        order_by="sort_order asc, brand_name asc",
    )

    # Count in one grouped query rather than one count per brand (N+1).
    counts = frappe.get_all(
        "Product",
        filters={"status": "Active", "brand": ["is", "set"]},
        fields=["brand", "count(name) as count"],
        group_by="brand",
    )
    count_by_brand = {c.brand: c.count for c in counts}

    for b in brands:
        b["count"] = count_by_brand.get(b["name"], 0)
    brands = [b for b in brands if b["count"] > 0]

    frappe.cache().set_value(cache_key, brands, expires_in_sec=300)
    return brands


@frappe.whitelist(allow_guest=True)
def get_category_products(slug, page=1, page_size=20, sort=None, in_stock=None):
    """Convenience endpoint: list products in a category by slug."""
    return list_products(category=slug, page=page, page_size=page_size,
                         sort=sort, in_stock=in_stock)


@frappe.whitelist(allow_guest=True)
def lookup_by_barcode(barcode):
    """Resolve a physical barcode → hub Product via Vendor Listing barcode."""
    guest_rate_limit("products.barcode", limit=100, window_seconds=60)
    if not barcode:
        frappe.throw(_("barcode is required"))

    # Check Vendor Listing barcode first (vendor-specific barcode)
    vl = frappe.db.get_value(
        "Vendor Listing",
        {"barcode": barcode, "status": "Active"},
        ["product", "vendor", "price"],
        as_dict=True,
    )
    if vl:
        doc = frappe.get_doc("Product", vl.product)
        return {
            "name": doc.name,
            "product_name": doc.product_name,
            "barcode": barcode,
            "sku": doc.sku,
            "price": flt(vl.price),
            "slug": doc.slug,
            "vendor": vl.vendor,
        }

    # Fallback to Product.sku (legacy)
    name = frappe.db.get_value("Product", {"sku": barcode, "status": "Active"}, "name")
    if not name:
        return None
    doc = frappe.get_doc("Product", name)
    return {
        "name": doc.name,
        "product_name": doc.product_name,
        "barcode": doc.sku,
        "sku": doc.sku,
        "price": flt(doc.price),
        "slug": doc.slug,
    }


@frappe.whitelist(allow_guest=True)
def create_vendor_listing(product, vendor, price=0, compare_price=0, barcode="", sku="",
                          status="Active", track_inventory=1, allow_backorder=0,
                          available_qty=0, priority=1, estimated_delivery_minutes=20,
                          delivery_zone=None, warehouse=""):
    """
    Create or update a Vendor Listing. Called by vendors (via hub_post, which
    authenticates with X-SM-Secret/X-Vendor-ID rather than a Frappe session —
    see verify_hub_secret) after successful barcode mapping.
    """
    guest_rate_limit("products.create_vendor_listing", limit=60, window_seconds=60)
    verify_hub_secret("products.create_vendor_listing")
    if not frappe.db.exists("Product", product):
        frappe.throw(_("Product {0} not found").format(product), frappe.DoesNotExistError)
    if not frappe.db.exists("Vendor", vendor):
        frappe.throw(_("Vendor {0} not found").format(vendor), frappe.DoesNotExistError)

    existing = frappe.db.get_value("Vendor Listing", {"product": product, "vendor": vendor}, "name")
    if existing:
        doc = frappe.get_doc("Vendor Listing", existing)
        doc.price = price
        doc.compare_price = compare_price
        if barcode:
            doc.barcode = barcode
        if sku:
            doc.sku = sku
        doc.status = status
        doc.track_inventory = track_inventory
        doc.allow_backorder = allow_backorder
        doc.available_qty = available_qty
        doc.priority = priority
        doc.estimated_delivery_minutes = estimated_delivery_minutes
        if delivery_zone:
            doc.delivery_zone = delivery_zone
        if warehouse:
            doc.warehouse = warehouse
        doc.save(ignore_permissions=True)
        return {"created": False, "name": doc.name}

    doc = frappe.new_doc("Vendor Listing")
    doc.product = product
    doc.vendor = vendor
    doc.price = price
    doc.compare_price = compare_price
    doc.barcode = barcode
    doc.sku = sku
    doc.status = status
    doc.track_inventory = track_inventory
    doc.allow_backorder = allow_backorder
    doc.available_qty = available_qty
    doc.reserved_qty = 0
    doc.priority = priority
    doc.estimated_delivery_minutes = estimated_delivery_minutes
    if delivery_zone:
        doc.delivery_zone = delivery_zone
    if warehouse:
        doc.warehouse = warehouse
    doc.insert(ignore_permissions=True)
    return {"created": True, "name": doc.name}


@frappe.whitelist(allow_guest=True)
def get_product_reviews(slug, page=1, page_size=10):
    """Get approved reviews for a product."""
    name = frappe.db.get_value("Product", {"slug": slug, "status": "Active"}, "name")
    if not name:
        frappe.throw(_("Product not found"), frappe.DoesNotExistError)

    page = max(1, int(page))
    page_size = min(50, max(1, int(page_size)))

    reviews = frappe.get_list(
        "Review",
        filters={"product": name, "status": "Approved"},
        fields=["name", "reviewer_name", "rating", "comment", "creation"],
        order_by="creation desc",
        limit_start=(page - 1) * page_size,
        limit_page_length=page_size,
    )

    stats = frappe.db.sql("""
        SELECT COUNT(*) as count, AVG(rating) as avg
        FROM `tabReview`
        WHERE product = %s AND status = 'Approved'
    """, (name,), as_dict=True)

    return {
        "reviews": reviews,
        "page": page,
        "page_size": page_size,
        "total": stats[0].count if stats else 0,
        "avg_rating": round(flt(stats[0].avg or 0), 1) if stats else 0,
    }


def get_effective_price(product_doc, price_type="Site Price", qty=1,
                        delivery_zone=None, vendor=None):
    """Resolve price from Vendor Listing, falling back to Product Price child table."""
    if vendor or delivery_zone:
        listings = frappe.get_list(
            "Vendor Listing",
            filters={"product": product_doc.name, "status": "Active"},
            fields=["vendor", "delivery_zone", "price"],
            order_by="delivery_zone ASC, price ASC",
        )
        if vendor:
            candidates = [l for l in listings if l.vendor == vendor]
            if delivery_zone:
                zoned = [l for l in candidates if l.delivery_zone == delivery_zone]
                if zoned:
                    return flt(zoned[0].price)
            base = [l for l in candidates if not l.delivery_zone]
            if base:
                return flt(base[0].price)
            if candidates:
                return flt(candidates[0].price)
        if delivery_zone:
            zoned = [l for l in listings if l.delivery_zone == delivery_zone]
            if zoned:
                return flt(zoned[0].price)

    if price_type != "Site Price" and hasattr(product_doc, "prices"):
        tier = next((
            p for p in product_doc.prices
            if p.price_type == price_type
            and p.is_active
            and flt(p.price) > 0
            and flt(qty) >= flt(p.min_qty or 1)
        ), None)
        if tier:
            return flt(tier.price)

    return flt(getattr(product_doc, "price", 0) or 0)


@frappe.whitelist(allow_guest=True)
def select_best_vendor(product_name, delivery_zone=None, customer_lat=None, customer_lng=None):
    """Select the best vendor for a product based on configurable logic."""
    if customer_lat is not None and customer_lng is not None:
        best = _get_best_vendor_listing(
            product_name,
            delivery_zone=delivery_zone,
            customer_lat=customer_lat,
            customer_lng=customer_lng,
        )
        if best:
            return best

    listings = frappe.get_list(
        "Vendor Listing",
        filters={"product": product_name, "status": "Active", "track_inventory": 1},
        fields=["name", "vendor", "price", "available_qty", "delivery_zone",
                "estimated_delivery_minutes", "priority"],
        order_by="priority desc, price asc",
    )
    if not listings:
        return None

    if delivery_zone:
        zone_listings = [l for l in listings if l.delivery_zone == delivery_zone]
        if zone_listings:
            in_stock = [l for l in zone_listings if flt(l.available_qty) > 0]
            return (in_stock or zone_listings)[0]

    in_stock = [l for l in listings if flt(l.available_qty) > 0]
    return (in_stock or listings)[0]


@frappe.whitelist(allow_guest=True)
def get_vendor_listings(product_slug):
    """Return all vendor listings for a product (public product page)."""
    name = frappe.db.get_value("Product", {"slug": product_slug, "status": "Active"}, "name")
    if not name:
        frappe.throw(_("Product not found"), frappe.DoesNotExistError)

    listings = frappe.get_list(
        "Vendor Listing",
        filters={"product": name},
        fields=["name", "vendor", "price", "compare_price", "available_qty",
                "reserved_qty", "track_inventory", "allow_backorder",
                "delivery_zone", "estimated_delivery_minutes", "priority",
                "sku", "vendor_product_id", "warehouse", "status",
                "last_updated", "last_sync_at"],
        order_by="priority desc, price asc",
    )

    # Resolve vendor names
    vendor_names = {}
    for l in listings:
        if l.vendor and l.vendor not in vendor_names:
            vendor_names[l.vendor] = frappe.db.get_value("Vendor", l.vendor, "vendor_name") or l.vendor

    result = []
    for l in listings:
        result.append({
            **l,
            "vendor_name": vendor_names.get(l.vendor, l.vendor),
        })

    return result


@frappe.whitelist(allow_guest=True)
def get_vendor_listings_by_location(product_slug, lat=None, lng=None, radius_km=10):
    """All vendors selling a product, sorted by distance from customer.

    Like middleware's get_franchise_listings but location-aware.
    Returns cheapest first within radius, then outside radius.
    """
    from saathimart.api.utils import guest_rate_limit
    guest_rate_limit("products.vendor_listings", limit=60, window_seconds=60)
    from saathimart.api.cart import _get_customer_location
    lat, lng = _get_customer_location(None, lat, lng)

    name = frappe.db.get_value("Product", {"slug": product_slug, "status": "Active"}, "name")
    if not name:
        frappe.throw(_("Product not found"), frappe.DoesNotExistError)

    # Get all vendor listings for this product
    listings = frappe.get_list(
        "Vendor Listing",
        filters={"product": name, "status": "Active"},
        fields=["name", "vendor", "price", "compare_price", "available_qty",
                "track_inventory", "delivery_zone", "estimated_delivery_minutes",
                "warehouse"],
        order_by="price asc",
    )

    if not listings:
        return []

    # Enrich with vendor location and distance
    vendor_names = set(l.vendor for l in listings if l.vendor)
    vendor_locations = {}
    if vendor_names and lat and lng:
        rows = frappe.db.sql(
            """SELECT name, vendor_name, lat, lng, service_radius_km,
                      ST_Distance_Sphere(
                          ST_PointFromText(CONCAT('POINT(', lng, ' ', lat, ')')),
                          ST_PointFromText(CONCAT('POINT(', %s, ' ', %s, ')'))
                      ) AS distance_meters
               FROM `tabVendor`
               WHERE name IN %s AND lat IS NOT NULL AND lng IS NOT NULL""",
            (lng, lat, tuple(vendor_names)),
            as_dict=True,
        )
        for r in rows:
            vendor_locations[r.name] = {
                "vendor_name": r.vendor_name or r.name,
                "lat": flt(r.lat),
                "lng": flt(r.lng),
                "distance_km": round(flt(r.distance_meters or 0) / 1000, 2),
                "service_radius_km": flt(r.service_radius_km or 5),
            }

    result = []
    for l in listings:
        loc = vendor_locations.get(l.vendor, {})
        result.append({
            "vendor": l.vendor,
            "vendor_name": loc.get("vendor_name", l.vendor),
            "price": flt(l.price),
            "compare_price": flt(l.compare_price or 0),
            "available_qty": flt(l.available_qty or 0),
            "in_stock": (not l.track_inventory) or flt(l.available_qty or 0) > 0,
            "delivery_zone": l.delivery_zone or "",
            "estimated_delivery_minutes": l.estimated_delivery_minutes or 30,
            "distance_km": loc.get("distance_km", 0),
            "lat": loc.get("lat"),
            "lng": loc.get("lng"),
            "service_radius_km": loc.get("service_radius_km", 5),
        })

    # Sort by distance if location provided, else by price
    if lat and lng:
        result.sort(key=lambda x: x.get("distance_km", 9999))
    else:
        result.sort(key=lambda x: x.get("price", 0))

    return result