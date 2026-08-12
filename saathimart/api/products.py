"""
Product API — public catalogue endpoints with Blinkit-style filtering and sorting.

All methods are whitelisted (allow_guest=True).
"""
import math

import frappe
from frappe import _
from frappe.utils import flt, nowdate, add_days, today
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
        SELECT name, vendor_name, lat, lng, service_radius_km,
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
        eligible = []
        fallback = []
        for l in listings:
            loc = vendor_location_map.get(l.vendor)
            if not loc:
                continue
            if loc.get("has_location") is False:
                continue
            if loc["distance_km"] <= loc["service_radius_km"]:
                if l.track_inventory and flt(stock_map.get(l.vendor, {}).get("available_qty") or 0) <= 0:
                    continue
                eligible.append(_enrich(l))
            else:
                fallback.append(_enrich(l))
        if eligible:
            eligible.sort(key=lambda x: x.distance_km)
            result = eligible[0]
            if _listings_map is None:
                frappe.cache().set_value(cache_key, result, expires_in_sec=300)
            return result
        if fallback and customer_lat is not None and customer_lng is not None:
            fallback.sort(key=lambda x: x.distance_km)
            result = fallback[0]
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

    best_listing = _get_best_vendor_listing(
        doc.name, vendor=vendor, delivery_zone=delivery_zone,
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
    }


@frappe.whitelist(allow_guest=True)
def list_products(category=None, vendor=None, search=None, page=1, page_size=20,
                  sort=None, in_stock=None, min_price=None, max_price=None, tags=None,
                  delivery_zone=None, lat=None, lng=None, radius_km=5):
    """
    Blinkit-style product listing with rich filters and sorting.

    Sort options:
      price_asc, price_desc, newest, popularity, rating

    Location params:
      lat, lng — customer coordinates; when provided, vendors are sorted by distance
      radius_km — maximum distance in km for a vendor to be considered (default: 5)
    """
    guest_rate_limit("products.list", limit=300, window_seconds=60)
    # Product has no is_active field of its own (only Category and the
    # Product Price child row do) — filtering on it here made Frappe
    # silently resolve it against the unrelated Product Price child table
    # and LEFT JOIN it in, duplicating rows per matching price row or
    # dropping products with no active price row at all. status covers
    # "is this product active" on its own, matching every other query below.
    filters = {"status": "Active"}

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
                "review_count", "brand"],
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
        best = _get_best_vendor_listing(
            p["name"], vendor=vendor, delivery_zone=delivery_zone,
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

        # Blinkit-style radius filter: only show products with a vendor within max_radius
        distance = flt(getattr(best, "distance_km", 0) or 0)
        has_location = getattr(best, "has_location", False)
        if max_radius is not None and has_location and distance > max_radius:
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

    cache_key = (
        f"sm_list_products:{category or ''}:{vendor or ''}:{search or ''}:"
        f"{page}:{page_size}:{sort or ''}:{lat or ''}:{lng or ''}:"
        f"{delivery_zone or ''}:{min_price or ''}:{max_price or ''}:"
        f"{in_stock or ''}:{tags or ''}:{radius_km or ''}"
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
    name = frappe.db.get_value("Product", {"slug": slug, "status": "Active"}, "name")
    if not name:
        frappe.throw(_("Product not found"), frappe.DoesNotExistError)

    cache_key = f"sm_product:{name}:{vendor or delivery_zone or 'hub'}:{lat or ''}:{lng or ''}:{radius_km or ''}"
    cached = frappe.cache().get_value(cache_key)
    if cached:
        return cached

    doc = frappe.get_doc("Product", name)
    clat = flt(lat) if lat is not None else None
    clng = flt(lng) if lng is not None else None
    max_radius = flt(radius_km) if radius_km is not None else None

    listings_map, stock_map, vendor_location_map = _preload_listing_data(
        [name], customer_lat=clat, customer_lng=clng
    )

    # Check if nearest vendor is within radius before serializing
    if clat is not None and clng is not None and max_radius is not None:
        best_check = _get_best_vendor_listing(
            name, vendor=vendor, delivery_zone=delivery_zone,
            customer_lat=clat, customer_lng=clng,
            _listings_map=listings_map, _stock_map=stock_map,
            _vendor_location_map=vendor_location_map,
        )
        if not best_check or flt(getattr(best_check, "distance_km", 0) or 0) > max_radius:
            frappe.throw(_("Product not available in your area"), frappe.DoesNotExistError)

    data = _serialize_product(doc, _listings_map=listings_map, _stock_map=stock_map,
                              _vendor_location_map=vendor_location_map,
                              vendor=vendor, delivery_zone=delivery_zone,
                              customer_lat=clat, customer_lng=clng)
    data["vendor_context"] = vendor or data.get("vendor")

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
        filters={"status": "Active", "category": doc.category, "name": ["!=", name]},
        fields=["name", "product_name", "slug", "category"],
        order_by="creation desc",
        limit_page_length=32,
    )
    related = random.sample(related_pool, min(8, len(related_pool)))
    data["related_products"] = [_serialize_product(frappe.get_doc("Product", p["name"])) for p in related]

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
