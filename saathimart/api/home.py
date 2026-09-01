"""
Homepage API — everything the Blinkit-style home screen needs.

Returns a single payload with:
  - banners       : active Hero / Promo banners
  - categories    : top-level categories for the quick-shop grid
  - deals         : products with compare_price > price (on sale)
  - bestsellers   : top-selling products by order volume
  - recommended   : "you may also like" — curated or random active products
  - quick_links   : shortcut category tiles (e.g. Fruits, Vegetables, Dairy)
"""
from __future__ import annotations

import json
import frappe
from frappe import _
from frappe.utils import today, add_days, nowdate, flt


def _serialize_product(row):
    """
    Turn a plain dict into a consistent frontend-friendly product dict.

    Only ever takes a dict now — there used to be a second branch here that
    accepted a raw Product Document and read doc.price/doc.compare_price/
    doc.stock_qty/doc.track_inventory/doc.vendor directly off it. None of
    those are real fields on Product (pricing lives on Vendor Listing,
    stock on Vendor Stock) — Frappe Documents return None for undefined
    attributes rather than raising, so that branch was silently emitting
    price=0 for every product it touched (see _get_recommended, its only
    caller) instead of erroring where you'd notice. Every caller now
    resolves price via Vendor Listing first and passes a dict, same as
    _get_deals/_get_bestsellers always did.
    """
    return {
        "name": row.get("name"),
        "product_name": row.get("product_name"),
        "slug": row.get("slug"),
        "price": flt(row.get("price") or 0),
        "compare_price": flt(row.get("compare_price") or 0),
        "thumbnail": row.get("thumbnail"),
        "stock_qty": flt(row.get("stock_qty") or 0),
        "track_inventory": row.get("track_inventory", 1),
        "category": row.get("category"),
        "vendor": row.get("vendor"),
        "short_description": row.get("short_description", ""),
        "is_on_sale": flt(row.get("compare_price") or 0) > flt(row.get("price") or 0),
        "discount_pct": 0,
    }


def _is_banner_active(banner):
    """Check if banner is within its validity window."""
    valid_from = banner.get("valid_from")
    valid_to = banner.get("valid_to")
    today_str = today()
    if valid_from and str(valid_from) > today_str:
        return False
    if valid_to and str(valid_to) < today_str:
        return False
    return True


@frappe.whitelist(allow_guest=True)
def get_homepage_data(lat=None, lng=None, radius_km=5):
    """
    Single-call homepage payload. Frontend calls this once on load.
    All sub-sections are optional — frontend should hide empty sections.

    Location params:
      lat, lng — customer coordinates; when provided, products are filtered
      to only include those with a vendor within radius_km (default: 5km)
    """
    # Resolve location from params or cart fallback (matching saathi_middleware)
    from saathimart.api.cart import _get_customer_location
    lat, lng = _get_customer_location(None, lat, lng)
    clat = flt(lat) if lat is not None else None
    clng = flt(lng) if lng is not None else None
    max_radius = flt(radius_km) if radius_km is not None else None

    data = {
        "banners": _get_banners(),
        "categories": _get_categories(),
        "deals": _get_deals(limit=20, customer_lat=clat, customer_lng=clng, max_radius=max_radius),
        "bestsellers": _get_bestsellers(limit=10, customer_lat=clat, customer_lng=clng, max_radius=max_radius),
        "recommended": _get_recommended(limit=12, customer_lat=clat, customer_lng=clng, max_radius=max_radius),
        "quick_links": _get_quick_links(),
        "announcement": _get_announcement(),
        "marketplace_banner": _get_marketplace_banner(),
    }
    return data


@frappe.whitelist(allow_guest=True)
def get_banners():
    """Public alias — returns active banners ordered by sort_order."""
    return _get_banners()


@frappe.whitelist(allow_guest=True)
def get_deals(limit=20, lat=None, lng=None, radius_km=5):
    """Products currently on sale (compare_price > price)."""
    return _get_deals(limit=int(limit), customer_lat=flt(lat) if lat is not None else None,
                      customer_lng=flt(lng) if lng is not None else None,
                      max_radius=flt(radius_km) if radius_km is not None else None)


@frappe.whitelist(allow_guest=True)
def get_bestsellers(limit=10, lat=None, lng=None, radius_km=5):
    """Top-selling products by quantity sold."""
    return _get_bestsellers(limit=int(limit), customer_lat=flt(lat) if lat is not None else None,
                            customer_lng=flt(lng) if lng is not None else None,
                            max_radius=flt(radius_km) if radius_km is not None else None)


@frappe.whitelist(allow_guest=True)
def get_recommended(limit=12, lat=None, lng=None, radius_km=5):
    """Random active products."""
    return _get_recommended(limit=int(limit), customer_lat=flt(lat) if lat is not None else None,
                            customer_lng=flt(lng) if lng is not None else None,
                            max_radius=flt(radius_km) if radius_km is not None else None)


@frappe.whitelist(allow_guest=True)
def get_quick_links():
    """Shortcut category tiles for the home quick-shop grid."""
    return _get_quick_links()


# ── Private helpers ────────────────────────────────────────────────────────────

def _get_banners():
    banners = frappe.get_list(
        "Banner",
        filters={"is_active": 1},
        fields=["name", "title", "banner_type", "heading", "subheading",
                "cta_label", "cta_url", "cta_secondary_label", "cta_secondary_url",
                "image", "mobile_image", "bg_color", "text_color", "sort_order",
                "valid_from", "valid_to"],
        order_by="sort_order asc",
    )
    result = []
    for b in banners:
        if not _is_banner_active(b):
            continue
        result.append({
            "id": frappe.scrub(b["title"]).replace("_", "-"),
            "title": b["title"],
            "type": b["banner_type"],
            "heading": b["heading"],
            "subheading": b["subheading"],
            "cta_label": b["cta_label"],
            "cta_url": b["cta_url"],
            "cta_secondary_label": b["cta_secondary_label"],
            "cta_secondary_url": b["cta_secondary_url"],
            "image": b["image"],
            "mobile_image": b["mobile_image"],
            "bg_color": b["bg_color"],
            "text_color": b["text_color"],
        })
    return result


def _get_categories():
    cats = frappe.get_list(
        "Category",
        filters={"is_active": 1},
        fields=["name", "category_name", "slug", "image", "parent_category", "sort_order"],
        order_by="sort_order asc, category_name asc",
    )
    top = [c for c in cats if not c.get("parent_category")]
    return top


def _get_deals(limit=20, customer_lat=None, customer_lng=None, max_radius=None):
    # Query Vendor Listing for active listings with compare_price > price
    listings = frappe.db.sql("""
        SELECT vl.product, vl.price, vl.compare_price, p.product_name, p.slug,
               p.thumbnail, p.category, p.short_description, p.tags
        FROM `tabVendor Listing` vl
        JOIN `tabProduct` p ON vl.product = p.name
        WHERE vl.status = 'Active'
          AND vl.compare_price > vl.price
          AND p.status = 'Active'
        ORDER BY vl.price ASC
        LIMIT %s
    """, (limit * 2,), as_dict=True)

    on_sale = []
    for l in listings[:limit]:
        # Blinkit-style radius filter
        if customer_lat is not None and customer_lng is not None and max_radius is not None:
            from saathimart.api.products import _get_best_vendor_listing, _preload_listing_data
            listings_map, stock_map, vendor_location_map = _preload_listing_data(
                [l.product], customer_lat=customer_lat, customer_lng=customer_lng
            )
            best = _get_best_vendor_listing(
                l.product, customer_lat=customer_lat, customer_lng=customer_lng,
                _listings_map=listings_map, _stock_map=stock_map,
                _vendor_location_map=vendor_location_map,
            )
            if not best or flt(getattr(best, "distance_km", 0) or 0) > max_radius:
                continue

        on_sale.append({
            "name": l.product,
            "product_name": l.product_name,
            "slug": l.slug,
            "price": flt(l.price),
            "compare_price": flt(l.compare_price),
            "thumbnail": l.thumbnail,
            "stock_qty": 0,
            "track_inventory": 1,
            "category": l.category,
            "vendor": None,
            "short_description": l.short_description or "",
            "is_on_sale": True,
            "discount_pct": round(((flt(l.compare_price) - flt(l.price)) / flt(l.compare_price)) * 100, 1) if flt(l.compare_price) > 0 else 0,
        })
    return on_sale


def _get_bestsellers(limit=10, customer_lat=None, customer_lng=None, max_radius=None):
    # Product carries no live price of its own — Vendor Listing does, one
    # row per (vendor, product) — but it does carry display_price/
    # display_compare_price, a cache kept in sync by
    # saathimart.events.publisher.on_vendor_listing_changed whenever any
    # Vendor Listing for a product changes. That's what a plain read here
    # is against, same idea as keeping price directly on
    # the row it queries, without giving up Vendor Listing as the real
    # source of truth for checkout. (This function used to select p.price/
    # p.compare_price directly off tabProduct — columns that never
    # existed there — which threw a raw MySQLdb.OperationalError and
    # 500'd the whole homepage payload, every single call.)
    data = frappe.db.sql("""
        SELECT oi.product, p.product_name, p.slug, p.thumbnail, p.category,
               p.display_price as price, p.display_compare_price as compare_price,
               SUM(oi.qty) as total_qty
        FROM `tabOrder Item` oi
        JOIN `tabOrder` o ON oi.parent = o.name
        LEFT JOIN `tabProduct` p ON oi.product = p.name
        WHERE o.creation >= %s
          AND o.status NOT IN ('Cancelled', 'Refunded')
        GROUP BY oi.product
        ORDER BY total_qty DESC
        LIMIT %s
    """, (add_days(today(), -30), limit), as_dict=True)

    result = []
    for r in data:
        if not r.price:
            continue  # no active listing anywhere — nothing to actually sell right now

        # Blinkit-style radius filter
        if customer_lat is not None and customer_lng is not None and max_radius is not None:
            from saathimart.api.products import _get_best_vendor_listing, _preload_listing_data
            listings_map, stock_map, vendor_location_map = _preload_listing_data(
                [r.product], customer_lat=customer_lat, customer_lng=customer_lng
            )
            best = _get_best_vendor_listing(
                r.product, customer_lat=customer_lat, customer_lng=customer_lng,
                _listings_map=listings_map, _stock_map=stock_map,
                _vendor_location_map=vendor_location_map,
            )
            if not best or flt(getattr(best, "distance_km", 0) or 0) > max_radius:
                continue

        result.append(_serialize_product({
            "name": r.product,
            "product_name": r.product_name,
            "slug": r.slug,
            "price": r.price,
            "compare_price": r.compare_price,
            "thumbnail": r.thumbnail,
            "category": r.category,
            "vendor": None,
            "short_description": "",
            "stock_qty": 0,
            "track_inventory": 1,
        }))
    return result


def _get_recommended(limit=12, customer_lat=None, customer_lng=None, max_radius=None):
    # frappe.get_list's order_by no longer accepts raw SQL like "rand()" (v16
    # validates the field format) — sample randomly in Python from a larger
    # pool instead.
    import random

    # display_price is the same cached-from-Vendor-Listing field
    # _get_bestsellers reads — see its docstring. Filtering status=Active
    # AND display_price>0 in the query itself (rather than fetching a
    # random pool and discarding zero-price rows after the fact) means the
    # random sample is actually drawn from products someone can buy.
    pool = frappe.get_list(
        "Product",
        filters={"status": "Active", "display_price": [">", 0]},
        fields=["name", "product_name", "slug", "category", "thumbnail",
                "short_description", "display_price", "display_compare_price"],
        order_by="creation desc",
        limit_page_length=max(limit * 4, 50),
    )
    sample = random.sample(pool, min(limit, len(pool)))

    result = []
    for p in sample:
        # Blinkit-style radius filter
        if customer_lat is not None and customer_lng is not None and max_radius is not None:
            from saathimart.api.products import _get_best_vendor_listing, _preload_listing_data
            listings_map, stock_map, vendor_location_map = _preload_listing_data(
                [p["name"]], customer_lat=customer_lat, customer_lng=customer_lng
            )
            best = _get_best_vendor_listing(
                p["name"], customer_lat=customer_lat, customer_lng=customer_lng,
                _listings_map=listings_map, _stock_map=stock_map,
                _vendor_location_map=vendor_location_map,
            )
            if not best or flt(getattr(best, "distance_km", 0) or 0) > max_radius:
                continue

        result.append(_serialize_product({
            "name": p["name"],
            "product_name": p["product_name"],
            "slug": p["slug"],
            "price": p["display_price"],
            "compare_price": p["display_compare_price"],
            "thumbnail": p["thumbnail"],
            "category": p["category"],
            "vendor": None,
            "short_description": p["short_description"] or "",
            "stock_qty": 0,
            "track_inventory": 1,
        }))
    return result


def _get_quick_links():
    """
    Return categories as quick-link tiles.
    In a real Blinkit-like app this could include curated links to
    specific product collections or promotional landing pages.
    """
    cats = frappe.get_list(
        "Category",
        filters={"is_active": 1, "parent_category": ["is", "not set"]},
        fields=["name", "category_name", "slug", "image", "sort_order"],
        order_by="sort_order asc, category_name asc",
        limit_page_length=12,
    )
    return [
        {
            "slug": c.slug,
            "label": c.category_name,
            "image": c.image,
            "href": f"/category/{c.slug}",
        }
        for c in cats
    ]


def _get_announcement():
    """Return a single announcement-bar string from Site Config if available."""
    try:
        from saathimart.api.cms import get_site_config
        config = get_site_config()
        text = config.get("navbar_announcement_text") or ""
        return {"text": text} if text else None
    except Exception:
        return None


def _get_marketplace_banner():
    """Marketplace-specific promo banner for the homepage.

    Uses the existing Hero Slide doctype with type='marketplace' or falls
    back to the first active hero slide. Shows "Shop from multiple vendors"
    style messaging.
    """
    try:
        # Try marketplace-specific slide first
        slide = frappe.db.get_value(
            "Hero Slide",
            {"is_active": 1, "slide_type": "marketplace"},
            ["title", "subtitle", "image", "link", "button_text"],
            as_dict=True,
        )
        if slide:
            return {
                "title": slide.title or "Shop from Multiple Vendors",
                "subtitle": slide.subtitle or "Best prices from vendors near you",
                "image": slide.image or "",
                "link": slide.link or "",
                "button_text": slide.button_text or "Shop Now",
            }
    except Exception:
        pass

    return None
