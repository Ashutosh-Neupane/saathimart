"""
Filters API — returns available filter values for the product listing sidebar.

Blinkit-style filter sidebar needs:
  - Categories (with product counts)
  - Price ranges (with product counts)
  - Brands / Vendors (with product counts)
  - Ratings (with product counts)
  - Dietary / Tags (with product counts)

All endpoints are guest-accessible.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt


@frappe.whitelist(allow_guest=True)
def get_filters(category=None, search=None, delivery_zone=None):
    """
    Return all filter options with product counts for the current catalog view.

    Response shape:
    {
      "categories": [{"name": "...", "slug": "...", "count": 42}, ...],
      "price_ranges": [{"label": "Under NPR 100", "min": 0, "max": 100, "count": 12}, ...],
      "vendors": [{"name": "...", "label": "...", "count": 8}, ...],
      "ratings": [{"rating": 4, "count": 15}, ...],
      "tags": [{"tag": "organic", "count": 5}, ...],
    }
    """
    base_filters = {"status": "Active"}
    if category:
        cat_name = frappe.db.get_value("Category", {"slug": category}, "name")
        if cat_name:
            base_filters["category"] = cat_name

    or_filters = None
    if search:
        or_filters = [
            ["product_name", "like", f"%{search}%"],
            ["tags", "like", f"%{search}%"],
            ["short_description", "like", f"%{search}%"],
        ]

    # Get products with their best vendor listing
    products = frappe.get_list(
        "Product",
        filters=base_filters,
        or_filters=or_filters,
        fields=["name", "category", "tags", "brand"],
    )

    # Enrich with vendor listing data
    product_data = []
    for p in products:
        best = _get_best_vendor_listing(p.name, delivery_zone=delivery_zone)
        if not best:
            continue
        p["price"] = flt(best.price)
        p["compare_price"] = flt(best.compare_price or 0)
        p["vendor"] = best.vendor
        p["vendor_name"] = frappe.db.get_value("Vendor", best.vendor, "vendor_name") if best.vendor else ""
        product_data.append(p)

    return {
        "categories": _count_by_field(product_data, "category", "Category", "category_name", "slug"),
        "vendors": _count_vendors(product_data),
        "tags": _count_tags(product_data),
        "price_ranges": _count_by_price_range(product_data),
        "ratings": _count_by_rating(),
    }


def _get_best_vendor_listing(product_name, delivery_zone=None):
    """Get best vendor listing for a product."""
    from saathimart.api.products import _get_best_vendor_listing as _get_best
    return _get_best(product_name, delivery_zone=delivery_zone)


def _count_by_field(products, field, doctype, label_field, slug_field=None):
    """Group products by a field and return counts."""
    counts = {}
    for p in products:
        val = p.get(field)
        if not val:
            continue
        counts[val] = counts.get(val, 0) + 1

    result = []
    for name, count in sorted(counts.items(), key=lambda x: x[1], reverse=True):
        label = name
        slug = name
        try:
            doc = frappe.get_doc(doctype, name)
            label = getattr(doc, label_field, name)
            if slug_field:
                slug = getattr(doc, slug_field, name)
        except Exception:
            pass
        result.append({"name": name, "label": label, "slug": slug, "count": count})
    return result


def _count_vendors(products):
    """Count products per vendor using vendor listing data."""
    counts = {}
    vendor_names = {}
    for p in products:
        v = p.get("vendor")
        if not v:
            continue
        counts[v] = counts.get(v, 0) + 1
        vendor_names[v] = p.get("vendor_name") or v

    result = []
    for name, count in sorted(counts.items(), key=lambda x: x[1], reverse=True):
        result.append({
            "name": name,
            "label": vendor_names.get(name, name),
            "count": count,
        })
    return result


def _count_tags(products):
    """Count products per tag (comma-separated)."""
    counts = {}
    for p in products:
        tags = (p.get("tags") or "").lower()
        for tag in [t.strip() for t in tags.split(",") if t.strip()]:
            counts[tag] = counts.get(tag, 0) + 1
    return [{"tag": tag, "count": count} for tag, count in sorted(counts.items(), key=lambda x: x[1], reverse=True)]


def _count_by_price_range(products):
    """Bucket products into Blinkit-style price ranges."""
    ranges = [
        {"label": "Under NPR 100",    "min": 0,    "max": 100,  "count": 0},
        {"label": "NPR 100 - 250",   "min": 100,  "max": 250,  "count": 0},
        {"label": "NPR 250 - 500",   "min": 250,  "max": 500,  "count": 0},
        {"label": "NPR 500 - 1000",  "min": 500,  "max": 1000, "count": 0},
        {"label": "Over NPR 1000",   "min": 1000, "max": 999999, "count": 0},
    ]
    for p in products:
        price = flt(p.get("price") or 0)
        for r in ranges:
            if r["min"] <= price <= r["max"]:
                r["count"] += 1
                break
    return [r for r in ranges if r["count"] > 0]


def _count_by_rating():
    """Return product counts per rating bucket."""
    rows = frappe.db.sql("""
        SELECT rating, COUNT(*) as count
        FROM `tabReview`
        WHERE status = 'Approved'
        GROUP BY rating
    """, as_dict=True)
    result = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    for r in rows:
        result[int(r.rating)] = r.count
    return [{"rating": k, "label": f"{k} ★ & up", "count": v} for k, v in sorted(result.items()) if v > 0]
