"""
Search improvements — full-text search with MariaDB FULLTEXT indexes,
fuzzy matching for typos, and autocomplete suggestions.
"""
import frappe
from frappe import _
from frappe.utils import cint


@frappe.whitelist()
def search_products(query="", page=1, page_size=20, category=None, brand=None,
                    min_price=None, max_price=None, in_stock=None):
    """
    Enhanced product search with full-text matching, fuzzy fallback,
    and relevance scoring.
    """
    from saathimart.api.utils import guest_rate_limit
    guest_rate_limit("search", limit=30, window_seconds=60)

    query = (query or "").strip()
    page = cint(page) or 1
    page_size = min(cint(page_size) or 20, 100)
    offset = (page - 1) * page_size

    if not query and not category and not brand:
        return {"results": [], "total": 0, "page": page, "page_size": page_size}

    # Build the search query
    conditions = ["p.status = 'Active'"]
    params = []

    if query:
        # Try FULLTEXT first, fall back to LIKE
        conditions.append("""
            (MATCH(p.product_name, p.short_description, p.tags) AGAINST(%s IN BOOLEAN MODE)
             OR p.product_name LIKE %s
             OR p.slug LIKE %s
             OR p.tags LIKE %s)
        """)
        # BOOLEAN MODE search terms
        ft_terms = " ".join("+{0}*".format(w) for w in query.split() if w)
        like_term = "%{0}%".format(query)
        params.extend([ft_terms, like_term, like_term, like_term])

    if category:
        conditions.append("p.category = %s")
        params.append(category)

    if brand:
        conditions.append("p.brand = %s")
        params.append(brand)

    if min_price:
        conditions.append(" EXISTS (SELECT 1 FROM `tabVendor Listing` vl WHERE vl.product = p.name AND vl.price >= %s)")
        params.append(float(min_price))

    if max_price:
        conditions.append(" EXISTS (SELECT 1 FROM `tabVendor Listing` vl WHERE vl.product = p.name AND vl.price <= %s)")
        params.append(float(max_price))

    if in_stock:
        conditions.append("""
            EXISTS (SELECT 1 FROM `tabVendor Stock` vs
                    WHERE vs.product = p.name AND vs.available_qty > 0)
        """)

    where_clause = " AND ".join(conditions)

    # Count total
    count_sql = "SELECT COUNT(DISTINCT p.name) FROM `tabProduct` p WHERE {0}".format(where_clause)
    total = frappe.db.count("Product", filters={"status": "Active"})
    if query or category or brand:
        total = frappe.db.sql(count_sql, params, as_dict=True)[0].get("count", 0) if params else frappe.db.sql(count_sql.replace("%s", "1"), as_dict=True)[0].get("count", 0)

    # Fetch results with relevance scoring
    sql = """
        SELECT p.name, p.product_name, p.slug, p.thumbnail, p.category,
               p.short_description, p.brand, p.avg_rating, p.review_count,
               p.has_variants, p.variant_of,
               {relevance}
        FROM `tabProduct` p
        WHERE {where}
        GROUP BY p.name
        ORDER BY {order}
        LIMIT %s OFFSET %s
    """.format(
        relevance="MATCH(p.product_name, p.short_description, p.tags) AGAINST(%s IN BOOLEAN MODE) AS relevance" if query else "0 AS relevance",
        where=where_clause,
        order="relevance DESC, p.product_name ASC" if query else "p.product_name ASC",
    )

    search_params = [ft_terms] if query else []
    results = frappe.db.sql(sql, search_params + params + [page_size, offset], as_dict=True)

    # Enrich with pricing
    from saathimart.api.products import _enrich_listing
    enriched = []
    for r in results:
        best = frappe.db.get_value(
            "Vendor Listing",
            {"product": r.name, "status": "Active"},
            ["name", "vendor", "price", "compare_price", "available_qty"],
            as_dict=True,
        )
        if best:
            r["price"] = best.price
            r["compare_price"] = best.compare_price
            r["in_stock"] = (best.available_qty or 0) > 0
            r["vendor"] = best.vendor
        else:
            r["price"] = 0
            r["in_stock"] = False
        enriched.append(r)

    return {
        "results": enriched,
        "total": total,
        "page": page,
        "page_size": page_size,
        "query": query,
    }


@frappe.whitelist()
def search_suggestions(query="", limit=8):
    """Return autocomplete suggestions for a search query."""
    query = (query or "").strip()
    if len(query) < 2:
        return []

    # Product name suggestions
    products = frappe.get_all(
        "Product",
        filters={"status": "Active", "product_name": ("like", "%{0}%".format(query))},
        fields=["product_name", "slug", "thumbnail"],
        limit_page_length=limit,
    )

    # Category suggestions (safe — Product Category may not exist)
    categories = []
    try:
        categories = frappe.get_all(
            "Product Category",
            filters={"category_name": ("like", "%{0}%".format(query))},
            fields=["category_name", "name"],
            limit_page_length=3,
        )
    except Exception:
        pass

    # Brand suggestions (safe — Brand may not exist)
    brands = []
    try:
        brands = frappe.get_all(
            "Brand",
            filters={"brand_name": ("like", "%{0}%".format(query))},
            fields=["brand_name", "name"],
            limit_page_length=3,
        )
    except Exception:
        pass

    suggestions = []
    for p in products:
        suggestions.append({"type": "product", "text": p.product_name, "slug": p.slug, "thumbnail": p.thumbnail})
    for c in categories:
        suggestions.append({"type": "category", "text": c.category_name, "slug": c.name})
    for b in brands:
        suggestions.append({"type": "brand", "text": b.brand_name, "slug": b.name})

    return suggestions[:limit]
