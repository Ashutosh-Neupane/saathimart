"""
Substitution Suggestions API — suggest alternatives when items are out of stock.

When a customer's preferred item is unavailable, suggesting alternatives
prevents cart abandonment and increases conversion.

Endpoints:
  - get_substitutions():     Get alternative products for an out-of-stock item
  - get_similar_products():  Get similar products from same category/brand
"""
import frappe
from frappe import _
from frappe.utils import flt
from saathimart.api.responses import handle_api_errors


# ── API Endpoints ──────────────────────────────────────────────────────────

@frappe.whitelist(allow_guest=True)
@handle_api_errors
def get_substitutions(product_name, lat=None, lng=None, limit=5):
    """Get substitute products for an out-of-stock item.

    Finds alternatives from the same category, preferring:
    1. Same brand, similar price
    2. Same category, similar price
    3. Same category, any price

    Args:
        product_name: The out-of-stock product name
        lat: Customer latitude for distance-aware results
        lng: Customer longitude for distance-aware results
        limit: Max substitutes to return (default 5)

    Returns:
        list of substitute products with relevance scores
    """
    limit = min(cint(limit) or 5, 10)

    # Get the original product info
    original = frappe.get_doc("Product", product_name)
    category = original.category
    brand = getattr(original, "brand", None)
    price = flt(original.price or 0)

    # Price range: ±30% of original price
    price_min = price * 0.7 if price > 0 else 0
    price_max = price * 1.3 if price > 0 else 999999

    substitutes = []
    seen = {product_name}  # Don't suggest the original

    # Strategy 1: Same brand, similar price (highest relevance)
    if brand:
        same_brand = frappe.get_all(
            "Product",
            filters={
                "brand": brand,
                "status": "Active",
                "name": ["not in", list(seen)],
            },
            fields=["name", "product_name", "slug", "price", "category", "thumbnail"],
            limit_page_length=limit * 2,
        )
        for p in same_brand:
            p_price = flt(p.price or 0)
            if price_min <= p_price <= price_max:
                substitutes.append({
                    "product": p.name,
                    "product_name": p.product_name,
                    "slug": p.slug,
                    "price": p_price,
                    "category": p.category,
                    "thumbnail": p.thumbnail,
                    "reason": "same_brand",
                    "relevance": 100,
                })
                seen.add(p.name)

    # Strategy 2: Same category, similar price
    if category and len(substitutes) < limit:
        same_cat = frappe.get_all(
            "Product",
            filters={
                "category": category,
                "status": "Active",
                "name": ["not in", list(seen)],
            },
            fields=["name", "product_name", "slug", "price", "category", "thumbnail"],
            order_by="avg_rating desc",
            limit_page_length=limit * 2,
        )
        for p in same_cat:
            p_price = flt(p.price or 0)
            if price_min <= p_price <= price_max:
                substitutes.append({
                    "product": p.name,
                    "product_name": p.product_name,
                    "slug": p.slug,
                    "price": p_price,
                    "category": p.category,
                    "thumbnail": p.thumbnail,
                    "reason": "same_category",
                    "relevance": 80,
                })
                seen.add(p.name)

    # Strategy 3: Same category, any price (if still need more)
    if category and len(substitutes) < limit:
        any_price = frappe.get_all(
            "Product",
            filters={
                "category": category,
                "status": "Active",
                "name": ["not in", list(seen)],
            },
            fields=["name", "product_name", "slug", "price", "category", "thumbnail"],
            order_by="avg_rating desc",
            limit_page_length=limit - len(substitutes),
        )
        for p in any_price:
            p_price = flt(p.price or 0)
            # Calculate price difference percentage
            price_diff_pct = abs(p_price - price) / price * 100 if price > 0 else 0
            substitutes.append({
                "product": p.name,
                "product_name": p.product_name,
                "slug": p.slug,
                "price": p_price,
                "category": p.category,
                "thumbnail": p.thumbnail,
                "reason": "same_category_any_price",
                "relevance": max(40, 70 - int(price_diff_pct)),
            })
            seen.add(p.name)

    # Sort by relevance
    substitutes.sort(key=lambda x: x["relevance"], reverse=True)

    return {
        "original_product": product_name,
        "original_price": price,
        "substitutes": substitutes[:limit],
    }


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def get_similar_products(product_name, limit=5):
    """Get similar products based on category and price range.

    Simpler than get_substitutions — just finds related products
    for the "You might also like" section.

    Args:
        product_name: Product to find similar items for
        limit: Max results (default 5)

    Returns:
        list of similar products
    """
    limit = min(cint(limit) or 5, 10)

    original = frappe.get_doc("Product", product_name)
    category = original.category
    price = flt(original.price or 0)
    price_min = price * 0.5 if price > 0 else 0
    price_max = price * 2.0 if price > 0 else 999999

    # Find products in same category, excluding original
    similar = frappe.get_all(
        "Product",
        filters={
            "category": category,
            "status": "Active",
            "name": ["!=", product_name],
        },
        fields=["name", "product_name", "slug", "price", "category", "thumbnail",
                "avg_rating", "review_count"],
        order_by="avg_rating desc, review_count desc",
        limit_page_length=limit + 5,  # Get extras for filtering
    )

    # Enrich with vendor listing availability
    result = []
    for p in similar:
        p_price = flt(p.price or 0)

        # Check if product has active vendor listing
        has_stock = frappe.db.exists(
            "Vendor Listing",
            {"product": p.name, "status": "Active"}
        )

        if has_stock:
            result.append({
                "product": p.name,
                "product_name": p.product_name,
                "slug": p.slug,
                "price": p_price,
                "category": p.category,
                "thumbnail": p.thumbnail,
                "avg_rating": flt(p.avg_rating or 0),
                "review_count": p.review_count or 0,
            })

        if len(result) >= limit:
            break

    return {
        "original_product": product_name,
        "similar_products": result,
    }


# Import cint at module level
from frappe.utils import cint
