"""
Search improvements — full-text search with MariaDB FULLTEXT indexes,
fuzzy matching for typos, and autocomplete suggestions.
"""
import frappe
from frappe import _
from frappe.utils import cint
from saathimart.api.responses import handle_api_errors


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def search_products(query="", page=1, page_size=20, category=None, brand=None,
                    min_price=None, max_price=None, in_stock=None,
                    lat=None, lng=None):
    """
    Enhanced product search with full-text matching, fuzzy fallback,
    and relevance scoring.
    """
    from saathimart.api.utils import guest_rate_limit
    guest_rate_limit("search", limit=30, window_seconds=60)

    # Resolve location from params or cart fallback (matching saathi_middleware)
    from saathimart.api.cart import _get_customer_location
    lat, lng = _get_customer_location(None, lat, lng)

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
        # LIKE keeps search usable on sites that have not created the optional
        # FULLTEXT index yet (including fresh native SaathiMart installs).
        conditions.append("""
            (p.product_name LIKE %s
             OR p.slug LIKE %s
             OR p.tags LIKE %s)
        """)
        like_term = "%{0}%".format(query)
        params.extend([like_term, like_term, like_term])

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

    if lat is not None and lng is not None:
        conditions.append("""
            EXISTS (
                SELECT 1
                FROM `tabVendor Listing` vl
                JOIN `tabVendor` v ON v.name = vl.vendor
                WHERE vl.product = p.name
                  AND vl.status = 'Active'
                  AND v.status = 'Active'
                  AND v.hub_status != 'Suspended'
                  AND v.lat IS NOT NULL AND v.lng IS NOT NULL
                  AND v.lat != 0 AND v.lng != 0
                  AND ST_Distance_Sphere(
                      ST_PointFromText(CONCAT('POINT(', v.lng, ' ', v.lat, ')')),
                      ST_PointFromText(CONCAT('POINT(', %s, ' ', %s, ')'))
                  ) <= COALESCE(NULLIF(v.service_radius_km, 0), 5) * 1000
            )
        """)
        params.extend([float(lng), float(lat)])

    where_clause = " AND ".join(conditions)

    # Count total
    count_sql = "SELECT COUNT(DISTINCT p.name) AS count FROM `tabProduct` p WHERE {0}".format(where_clause)
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
        relevance="0 AS relevance",
        where=where_clause,
        order="relevance DESC, p.product_name ASC" if query else "p.product_name ASC",
    )

    search_params = []
    results = frappe.db.sql(sql, search_params + params + [page_size, offset], as_dict=True)

    # Enrich with the best active vendor listing for this location, rather
    # than whichever listing the database happens to return first.
    from saathimart.api.products import _resolve_best_listing
    enriched = []
    for r in results:
        best = _resolve_best_listing(
            r.name,
            r.has_variants,
            customer_lat=lat,
            customer_lng=lng,
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

    # Record search for analytics (background job)
    if query:
        _record_search(query, total)

    return {
        "results": enriched,
        "total": total,
        "page": page,
        "page_size": page_size,
        "query": query,
    }


@frappe.whitelist()
@handle_api_errors
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


# ── Search Tracking ────────────────────────────────────────────────────────────

def normalize_search_key(query):
    """Normalize a search query for deduplication.

    Lowercases, strips whitespace, collapses multiple spaces.
    """
    return " ".join(query.lower().split()).strip()


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def get_top_searches(limit=10):
    """Most popular search terms — for empty-search-box suggestions.

    Falls back to best-selling items of the last 30 days if no searches
    have been recorded yet.
    """
    from frappe.utils import cint, add_days, today
    limit = min(50, max(1, cint(limit) or 10))

    terms = frappe.get_all(
        "SM Search Term",
        fields=["search_term", "search_count"],
        filters={"search_count": (">", 0)},
        order_by="search_count desc",
        limit_page_length=limit,
    )
    if terms:
        return [{"term": t.search_term, "count": cint(t.search_count)} for t in terms]

    # Fallback: recent bestsellers
    rows = frappe.db.sql(
        """
        SELECT oi.product_name, COUNT(*) AS order_count
        FROM `tabOrder Item` oi
        JOIN `tabOrder` o ON oi.parent = o.name
        WHERE o.creation >= %(since)s
          AND o.status NOT IN ('Cancelled', 'Refunded')
          AND oi.product_name IS NOT NULL AND oi.product_name != ''
        GROUP BY oi.product_name
        ORDER BY order_count DESC
        LIMIT %(limit)s
        """,
        {"since": add_days(today(), -30), "limit": limit},
        as_dict=True,
    )
    return [{"term": r.product_name, "count": cint(r.order_count)} for r in rows]


def record_search_term(key, term, result_count):
    """Background job — bump one term's counter.

    Uses INSERT ... ON DUPLICATE KEY UPDATE for concurrency safety.
    """
    from frappe.utils import cint, now_datetime
    try:
        frappe.db.sql(
            """
            INSERT INTO `tabSM Search Term`
                (name, search_key, search_term, search_count, last_result_count,
                 last_searched_at, creation, modified, owner, modified_by)
            VALUES
                (%(key)s, %(key)s, %(term)s, 1, %(count)s,
                 %(now)s, %(now)s, %(now)s, 'Administrator', 'Administrator')
            ON DUPLICATE KEY UPDATE
                search_count = search_count + 1,
                last_result_count = %(count)s,
                last_searched_at = %(now)s,
                modified = %(now)s
            """,
            {"key": key, "term": term, "count": cint(result_count), "now": now_datetime()},
        )
        frappe.db.commit()
    except Exception:
        frappe.log_error(f"Failed to record search term: {key}", "search_tracking")


def _record_search(query, result_count):
    """Queue analytics write for this search (background job)."""
    settings = frappe.get_single("Settings")
    if not getattr(settings, "search_tracking_enabled", 1):
        return

    key = normalize_search_key(query)
    if not key:
        return

    frappe.enqueue(
        "saathimart.api.search.record_search_term",
        queue="short",
        key=key,
        term=query.strip(),
        result_count=cint(result_count),
    )
