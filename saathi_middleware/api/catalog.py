"""
Public catalog API — categories/products/product-detail for the storefront
frontend. Distinct from item.get_nearby_items (which is a geolocation-first
"what's near me" feed): this is the general browse/search/filter surface
behind /categories and /products/[slug], with pagination, sorting and
price/stock filters, matching the shape the frontend's lib/data/catalog.ts
already expects.

"Product" here is a Saathi Item — this middleware has no cross-franchise
product identity, so a slug (== the item's docname, "<franchise>-<item_code>")
identifies one franchise's listing, not an abstract catalog product.

avg_rating/review_count come from Saathi Item's own denormalized fields
(kept in sync by SM Product Review's on_update/on_trash — see
api/reviews.py). compare_price/specifications still have no backing data
(no variant-pricing model) — returned as null/empty so the frontend's
existing types are satisfied without lying about data that doesn't exist.
"""
import json
import re

import frappe
from frappe.utils import flt, cint


def _slugify(value):
	if not value:
		return ""
	value = value.strip().lower()
	value = re.sub(r"[^a-z0-9]+", "-", value)
	return value.strip("-")


def _image_url(path):
	return frappe.utils.get_url(path) if path else None


def _category_doc_to_dict(doc):
	return {
		"name": doc.name,
		"category_name": doc.category_name,
		"slug": _slugify(doc.category_name),
		"image": _image_url(doc.image),
		"is_active": 1,
	}


def _item_row_to_product(row):
	# `category` is returned slugified, not as the raw Saathi Item Category
	# docname — the frontend uses BackendProduct.category directly as
	# CatalogProduct.categorySlug (lib/data/catalog.ts's mapBackendProduct),
	# and category links/list_products' `category` filter both key off the
	# slug, not the Frappe docname.
	category_slug = _slugify(row.get("category_name")) or _slugify(row.get("item_group"))

	gallery_urls = [_image_url(p) for p in (row.get("gallery_images") or [])]
	gallery_urls = [u for u in gallery_urls if u]

	return {
		"name": row["name"],
		"product_name": row["item_name"],
		"slug": row["name"],
		"category": category_slug,
		"category_name": row.get("category_name"),
		"brand": None,
		"status": "Active" if row.get("is_active") else "Inactive",
		"price": flt(row.get("price")),
		"compare_price": None,
		"stock_qty": flt(row.get("stock_qty")),
		"track_inventory": 1,
		"allow_backorder": 0,
		"thumbnail": _image_url(row.get("image")),
		"media": [],
		"images": json.dumps(gallery_urls) if gallery_urls else None,
		"short_description": row.get("description") or "",
		"description": row.get("description") or "",
		"meta_title": row.get("meta_title"),
		"meta_description": row.get("meta_description"),
		"tags": row.get("tags"),
		"vendor": row.get("franchise"),
		"vendor_product_id": row.get("item_code"),
		"delivery_zone": row.get("city"),
		"avg_rating": flt(row.get("avg_rating")),
		"review_count": cint(row.get("review_count")),
		"sku": row.get("item_code"),
		"specifications": [],
	}


@frappe.whitelist(allow_guest=True)
def list_categories():
	"""All Saathi Item Categories, slugified for frontend routing."""
	docs = frappe.get_all("Saathi Item Category", fields=["name", "category_name", "image"])
	return [_category_doc_to_dict(frappe._dict(d)) for d in docs]


_SORT_MAP = {
	"price_asc": "item.price ASC",
	"price_desc": "item.price DESC",
	"newest": "item.creation DESC",
	"rating": "item.avg_rating DESC",
}


@frappe.whitelist(allow_guest=True)
def list_products(
	category=None,
	search=None,
	page=1,
	page_size=20,
	sort=None,
	in_stock=None,
	min_price=None,
	max_price=None,
	tags=None,
	lat=None,
	lng=None,
):
	page = max(cint(page), 1)
	page_size = min(max(cint(page_size), 1), 100)

	conditions = ["item.is_active = 1", "franchise.status = 'Active'"]
	values = {}

	if category:
		# `category` may be a comma-separated list of slugs
		# (getProductsByCategorySlugs joins several). SQL can't slugify
		# category_name/item_group the same way _slugify() does (it strips
		# all punctuation, not just spaces), so resolve slug -> matching
		# category docnames / item_group values in Python first, then
		# filter on those directly.
		requested_slugs = {_slugify(s) for s in category.split(",") if s.strip()}

		matching_categories = [
			c.name for c in frappe.get_all("Saathi Item Category", fields=["name", "category_name"])
			if _slugify(c.category_name) in requested_slugs
		]
		matching_item_groups = [
			g.item_group for g in frappe.get_all(
				"Saathi Item", fields=["item_group"], distinct=True, filters={"item_group": ["is", "set"]}
			)
			if _slugify(g.item_group) in requested_slugs
		]

		if matching_categories or matching_item_groups:
			or_clauses = []
			if matching_categories:
				or_clauses.append("item.category IN %(matching_categories)s")
				values["matching_categories"] = matching_categories
			if matching_item_groups:
				or_clauses.append("item.item_group IN %(matching_item_groups)s")
				values["matching_item_groups"] = matching_item_groups
			conditions.append(f"({' OR '.join(or_clauses)})")
		else:
			# Requested slug(s) matched nothing — return no results rather
			# than silently ignoring the filter.
			conditions.append("1 = 0")

	if search:
		conditions.append("(item.item_name LIKE %(search)s OR item.description LIKE %(search)s)")
		values["search"] = f"%{search}%"

	if in_stock:
		conditions.append("item.stock_qty > 0")

	if min_price is not None and min_price != "":
		conditions.append("item.price >= %(min_price)s")
		values["min_price"] = flt(min_price)

	if max_price is not None and max_price != "":
		conditions.append("item.price <= %(max_price)s")
		values["max_price"] = flt(max_price)

	if tags:
		conditions.append("item.tags LIKE %(tags)s")
		values["tags"] = f"%{tags}%"

	where_clause = " AND ".join(conditions)
	order_by = _SORT_MAP.get(sort, "item.creation DESC")

	# When the caller has a location, this is the same "can this franchise
	# actually deliver here" filter get_nearby_items uses — without it,
	# the catalog would show items from franchises with no way to reach
	# the customer at all (this previously accepted lat/lng but silently
	# never used them). Distance becomes the default sort too, since
	# "closest first" is more useful than "newest first" once we know
	# where the customer is.
	distance_select = ""
	having_clause = ""
	if lat is not None and lng is not None:
		distance_select = """,
			(6371 * ACOS(
				LEAST(1, GREATEST(-1,
					COS(RADIANS(%(lat)s)) * COS(RADIANS(franchise.latitude)) *
					COS(RADIANS(franchise.longitude) - RADIANS(%(lng)s)) +
					SIN(RADIANS(%(lat)s)) * SIN(RADIANS(franchise.latitude))
				))
			)) AS distance_km"""
		having_clause = "HAVING distance_km <= franchise.serviceable_radius_km"
		values["lat"] = flt(lat)
		values["lng"] = flt(lng)
		if not sort:
			order_by = "distance_km ASC"

	total_rows = frappe.db.sql(
		f"""
		SELECT item.name, franchise.serviceable_radius_km {distance_select}
		FROM `tabSaathi Item` item
		JOIN `tabFranchise` franchise ON franchise.name = item.franchise
		LEFT JOIN `tabSaathi Item Category` cat ON cat.name = item.category
		WHERE {where_clause}
		{having_clause}
		""",
		values,
	)
	total = len(total_rows)

	rows = frappe.db.sql(
		f"""
		SELECT
			item.name, item.item_code, item.item_name, item.description,
			item.item_group, item.category, cat.category_name, item.uom,
			item.price, item.stock_qty, item.image, item.franchise,
			item.meta_title, item.meta_description, item.tags,
			item.avg_rating, item.review_count,
			item.is_active, item.city, item.latitude, item.longitude,
			franchise.serviceable_radius_km
			{distance_select}
		FROM `tabSaathi Item` item
		JOIN `tabFranchise` franchise ON franchise.name = item.franchise
		LEFT JOIN `tabSaathi Item Category` cat ON cat.name = item.category
		WHERE {where_clause}
		{having_clause}
		ORDER BY {order_by}
		LIMIT %(limit)s OFFSET %(offset)s
		""",
		{**values, "limit": page_size, "offset": (page - 1) * page_size},
		as_dict=True,
	)

	if rows:
		gallery_rows = frappe.db.sql(
			"""
			SELECT parent, image FROM `tabSaathi Item Image`
			WHERE parent IN %(parents)s
			ORDER BY idx ASC
			""",
			{"parents": [r["name"] for r in rows]},
			as_dict=True,
		)
		gallery_by_item = {}
		for g in gallery_rows:
			gallery_by_item.setdefault(g["parent"], []).append(g["image"])
		for row in rows:
			row["gallery_images"] = gallery_by_item.get(row["name"], [])

	items = [_item_row_to_product(row) for row in rows]

	return {"items": items, "total": total, "page": page, "page_size": page_size}


@frappe.whitelist(allow_guest=True)
def get_product(slug):
	if not frappe.db.exists("Saathi Item", slug):
		frappe.throw(frappe._("Product not found"), frappe.DoesNotExistError)

	doc = frappe.get_doc("Saathi Item", slug)
	category_name = None
	if doc.category:
		category_name = frappe.db.get_value("Saathi Item Category", doc.category, "category_name")

	row = doc.as_dict()
	row["category_name"] = category_name
	row["gallery_images"] = [i.image for i in (doc.gallery_images or [])]
	return _item_row_to_product(row)
