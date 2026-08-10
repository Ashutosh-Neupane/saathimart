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
    """Turn a Product doc / dict into a consistent frontend-friendly dict."""
    if isinstance(row, dict):
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
    doc = row
    price = flt(doc.price or 0)
    compare = flt(doc.compare_price or 0)
    discount = 0
    if compare > price:
        discount = round(((compare - price) / compare) * 100, 1)
    return {
        "name": doc.name,
        "product_name": doc.product_name,
        "slug": doc.slug,
        "price": price,
        "compare_price": compare,
        "thumbnail": doc.thumbnail,
        "stock_qty": flt(doc.stock_qty or 0),
        "track_inventory": doc.track_inventory,
        "category": doc.category,
        "vendor": doc.vendor,
        "short_description": doc.short_description or "",
        "is_on_sale": compare > price,
        "discount_pct": discount,
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
def get_homepage_data():
    """
    Single-call homepage payload. Frontend calls this once on load.
    All sub-sections are optional — frontend should hide empty sections.
    """
    data = {
        "banners": _get_banners(),
        "categories": _get_categories(),
        "deals": _get_deals(limit=20),
        "bestsellers": _get_bestsellers(limit=10),
        "recommended": _get_recommended(limit=12),
        "quick_links": _get_quick_links(),
        "announcement": _get_announcement(),
    }
    return data


@frappe.whitelist(allow_guest=True)
def get_banners():
    """Public alias — returns active banners ordered by sort_order."""
    return _get_banners()


@frappe.whitelist(allow_guest=True)
def get_deals(limit=20):
    """Products currently on sale (compare_price > price)."""
    return _get_deals(limit=int(limit))


@frappe.whitelist(allow_guest=True)
def get_bestsellers(limit=10):
    """Top-selling products by quantity sold."""
    return _get_bestsellers(limit=int(limit))


@frappe.whitelist(allow_guest=True)
def get_recommended(limit=12):
    """Curated product recommendations."""
    return _get_recommended(limit=int(limit))


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


def _get_deals(limit=20):
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


def _get_bestsellers(limit=10):
    data = frappe.db.sql("""
        SELECT oi.product, p.product_name, p.slug, p.price, p.compare_price,
               p.thumbnail, p.category, SUM(oi.qty) as total_qty
        FROM `tabOrder Item` oi
        JOIN `tabOrder` o ON oi.parent = o.name
        LEFT JOIN `tabProduct` p ON oi.product = p.name
        WHERE o.creation >= %s
          AND o.status NOT IN ('Cancelled', 'Refunded')
        GROUP BY oi.product
        ORDER BY total_qty DESC
        LIMIT %s
    """, (add_days(today(), -30), limit), as_dict=True)

    return [_serialize_product({
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
    }) for r in data]


def _get_recommended(limit=12):
    # frappe.get_list's order_by no longer accepts raw SQL like "rand()" (v16
    # validates the field format) — sample randomly in Python from a larger
    # pool instead.
    import random

    pool = frappe.get_list(
        "Product",
        filters={"status": "Active"},
        fields=["name", "product_name", "slug", "category"],
        order_by="creation desc",
        limit_page_length=max(limit * 4, 50),
    )
    sample = random.sample(pool, min(limit, len(pool)))
    return [_serialize_product(frappe.get_doc("Product", p["name"])) for p in sample]


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
