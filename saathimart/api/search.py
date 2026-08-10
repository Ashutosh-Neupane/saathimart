"""
Search API — Blinkit-style search with autocomplete, suggestions, and analytics.

Endpoints:
  autocomplete     — GET /api/method/saathimart.api.search.autocomplete
  search_products  — GET /api/method/saathimart.api.search.search_products
  get_top_searches — GET /api/method/saathimart.api.search.get_top_searches
  get_suggestions  — GET /api/method/saathimart.api.search.get_suggestions
"""
from __future__ import annotations

import json
import frappe
from frappe import _
from frappe.utils import add_days, flt, now_datetime, nowdate, today


def _normalize_search_key(query):
    """Lowercase, strip, collapse spaces."""
    return " ".join(query.lower().strip().split())


@frappe.whitelist(allow_guest=True)
def autocomplete(query, limit=10):
    """
    Instant search suggestions as user types.
    Returns matching product names and category names.
    """
    if not query or len(query.strip()) < 2:
        return {"suggestions": []}

    query = query.strip()
    limit = min(20, max(1, int(limit)))

    products = frappe.db.sql("""
        SELECT name, product_name, slug, thumbnail, price
        FROM `tabProduct`
        WHERE status = 'Active'
          AND (product_name LIKE %(q)s OR sku LIKE %(q)s OR tags LIKE %(q)s)
        LIMIT %(limit)s
    """, {"q": f"%{query}%", "limit": limit}, as_dict=True)

    categories = frappe.db.sql("""
        SELECT name, category_name, slug, image
        FROM `tabCategory`
        WHERE is_active = 1
          AND (category_name LIKE %(q)s OR slug LIKE %(q)s)
        LIMIT 5
    """, {"q": f"%{query}%"}, as_dict=True)

    suggestions = []
    for p in products:
        suggestions.append({
            "type": "product",
            "name": p.product_name,
            "slug": p.slug,
            "thumbnail": p.thumbnail,
            "price": flt(p.price or 0),
            "href": f"/products/{p.slug}",
        })
    for c in categories:
        suggestions.append({
            "type": "category",
            "name": c.category_name,
            "slug": c.slug,
            "image": c.image,
            "href": f"/category/{c.slug}",
        })

    return {"suggestions": suggestions[:limit]}


@frappe.whitelist(allow_guest=True)
def search_products(query, page=1, page_size=20, category=None, sort=None,
                   min_price=None, max_price=None, in_stock=None, tags=None):
    """
    Full product search with ranking.

    Ranks results by:
      1. Exact product_name match (highest)
      2. Name starts with query
      3. SKU match
      4. Tag match
      5. Description match (lowest)
    """
    if not query or len(query.strip()) < 2:
        return {"items": [], "page": page, "page_size": page_size, "total": 0}

    from saathimart.api.products import list_products, _serialize_product

    result = list_products(
        category=category,
        search=query,
        page=page,
        page_size=page_size,
        sort=sort,
        in_stock=in_stock,
        min_price=min_price,
        max_price=max_price,
        tags=tags,
    )
    items = result.get("items", [])

    # Record search query for analytics
    _record_search(query, len(items))

    return {
        "items": items,
        "page": result.get("page", page),
        "page_size": result.get("page_size", page_size),
        "total": result.get("total", 0),
        "query": query,
    }


@frappe.whitelist(allow_guest=True)
def get_top_searches(limit=10):
    """Return most searched terms from Ecommerce Search Analytics or fallback to popular products."""
    limit = min(50, max(1, int(limit)))

    # Try Search Analytics first
    try:
        analytics = frappe.get_list(
            "Ecommerce Search Analytics",
            fields=["search_term", "search_count"],
            order_by="search_count desc",
            limit_page_length=limit,
        )
        if analytics:
            return [{"term": r.search_term, "count": r.search_count} for r in analytics]
    except Exception:
        pass

    # Fallback: derive from product name/tag frequency in orders
    terms = frappe.db.sql("""
        SELECT oi.product, p.product_name, COUNT(*) as order_count
        FROM `tabOrder Item` oi
        JOIN `tabOrder` o ON oi.parent = o.name
        LEFT JOIN `tabProduct` p ON oi.product = p.name
        WHERE o.creation >= %s
          AND o.status NOT IN ('Cancelled', 'Refunded')
        GROUP BY oi.product
        ORDER BY order_count DESC
        LIMIT %s
    """, (add_days(today(), -30), limit), as_dict=True)

    return [{"term": r.product_name, "count": r.order_count} for r in terms if r.product_name]


@frappe.whitelist(allow_guest=True)
def get_suggestions(query, limit=10):
    """
    Alias for autocomplete — kept for backward compat with old frontend.
    """
    return autocomplete(query, limit=limit)


def _record_search(query, result_count):
    """Persist a search query for top-search analytics when tracking is enabled."""
    try:
        settings = frappe.get_single("Settings")
        if not getattr(settings, "track_top_searches", 0):
            return
    except Exception:
        return

    normalized = _normalize_search_key(query)
    if not normalized:
        return

    try:
        existing = frappe.db.get_value(
            "Ecommerce Search Analytics",
            {"search_key": normalized},
            ["name", "search_count", "last_result_count"],
            as_dict=True,
        )
        if existing:
            frappe.db.set_value("Ecommerce Search Analytics", existing.name, {
                "search_count": (existing.search_count or 0) + 1,
                "last_result_count": result_count,
                "last_searched_at": now_datetime(),
            })
        else:
            frappe.get_doc({
                "doctype": "Ecommerce Search Analytics",
                "search_term": query.strip(),
                "search_key": normalized,
                "search_count": 1,
                "last_result_count": result_count,
                "last_searched_at": now_datetime(),
            }).insert(ignore_permissions=True)
    except Exception:
        pass
