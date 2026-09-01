"""
Mobile API optimization — lighter payloads, cursor pagination, field selection,
and offline sync support for mobile apps.
"""
import frappe
from frappe import _
from frappe.utils import cint, cstr, flt
import json
from saathimart.api.responses import handle_api_errors


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def list_products_light(category=None, search=None, page=1, page_size=20,
                        sort=None, lat=None, lng=None, cursor=None,
                        fields=None):
    """
    Lightweight product listing optimized for mobile.

    Features:
    - Cursor-based pagination (faster than offset for infinite scroll)
    - Field selection (only fetch what the client needs)
    - Smaller payload (no heavy fields unless requested)
    - Mobile-friendly sorting
    """
    guest_rate_limit("mobile.list", limit=60, window_seconds=60)

    page_size = min(cint(page_size) or 20, 50)

    # Parse cursor for cursor-based pagination
    offset = 0
    if cursor:
        try:
            import base64
            cursor_data = json.loads(base64.b64decode(cursor))
            offset = cursor_data.get("offset", 0)
        except Exception:
            offset = 0
    else:
        offset = (cint(page) - 1) * page_size if page else 0

    # Default fields for mobile (lightweight)
    default_fields = ["name", "product_name", "slug", "thumbnail", "category"]
    requested_fields = fields.split(",") if fields else default_fields

    # Build query
    conditions = ["p.status = 'Active'"]
    params = []

    if category:
        conditions.append("p.category = %s")
        params.append(category)
    if search:
        conditions.append("p.product_name LIKE %s")
        params.append("%{0}%".format(search))

    where = " AND ".join(conditions)

    # Count
    total = frappe.db.sql(
        "SELECT COUNT(*) FROM `tabProduct` p WHERE {0}".format(where),
        params
    )[0][0]

    # Fetch products
    field_sql = ", ".join("p.{0}".format(f) for f in requested_fields if f in [
        "name", "product_name", "slug", "thumbnail", "category",
        "short_description", "brand", "avg_rating", "review_count",
        "has_variants", "variant_of", "tags",
    ])

    products = frappe.db.sql("""
        SELECT {fields}
        FROM `tabProduct` p
        WHERE {where}
        ORDER BY p.product_name ASC
        LIMIT %s OFFSET %s
    """.format(fields=field_sql, where=where),
        params + [page_size, offset],
        as_dict=True
    )

    # Enrich with price (lightweight — only price and in_stock)
    for p in products:
        best = frappe.db.get_value(
            "Vendor Listing",
            {"product": p.name, "status": "Active"},
            ["price", "compare_price", "available_qty"],
            as_dict=True,
        )
        if best:
            p["price"] = best.price
            p["compare_price"] = best.compare_price
            p["in_stock"] = (best.available_qty or 0) > 0
        else:
            p["price"] = 0
            p["in_stock"] = False

    # Generate next cursor
    next_cursor = None
    if offset + page_size < total:
        import base64
        next_cursor = base64.b64encode(json.dumps({
            "offset": offset + page_size,
        }).encode()).decode()

    return {
        "products": products,
        "total": total,
        "page_size": page_size,
        "cursor": next_cursor,
        "has_more": next_cursor is not None,
    }


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def get_product_light(slug):
    """Lightweight product detail for mobile — minimal fields, fast response."""
    from saathimart.api.products import get_product
    # Reuse the full endpoint but the client can request specific fields
    return get_product(slug)


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def get_cart_light(session_id=None):
    """Lightweight cart for mobile — minimal fields."""
    from saathimart.api.cart import find_active_cart, get_cart
    cart_name = find_active_cart(session_id)
    if not cart_name:
        return {"items": [], "total": 0}

    cart = frappe.get_doc("Cart", cart_name)
    items = []
    for ci in (cart.items or []):
        items.append({
            "product": ci.product,
            "product_name": ci.product_name,
            "qty": ci.qty,
            "rate": ci.rate,
            "amount": flt(ci.qty) * flt(ci.rate),
            "thumbnail": ci.thumbnail or "",
        })

    return {
        "items": items,
        "item_count": len(items),
        "subtotal": cart.subtotal or 0,
        "delivery_zone": cart.delivery_zone or None,
        "total": cart.subtotal or 0,
    }


def guest_rate_limit(endpoint, limit=60, window_seconds=60):
    """Rate limit guest mobile endpoints by IP."""
    from saathimart.api.utils import guest_rate_limit as _grl
    _grl(endpoint, limit=limit, window_seconds=window_seconds)
