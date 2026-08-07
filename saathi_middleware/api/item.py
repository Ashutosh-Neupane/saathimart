import frappe
from frappe.rate_limiter import rate_limit

from saathi_middleware.utils.auth import get_authenticated_franchise


def _upsert_item(franchise, payload):
	item_code = payload.get("item_code")
	if not item_code:
		return {"item_code": None, "status": "error", "message": "item_code is required"}

	docname = f"{franchise.name}-{item_code}"
	existing = frappe.db.exists("Item", docname)

	if existing:
		pushed_at = frappe.db.get_value("Item", docname, "pushed_at")
		if pushed_at and payload.get("pushed_at") and str(pushed_at) >= str(payload.get("pushed_at")):
			return {"item_code": item_code, "status": "skipped", "message": "stale update ignored"}
		doc = frappe.get_doc("Item", docname)
	else:
		doc = frappe.new_doc("Item")
		doc.franchise = franchise.name
		doc.item_code = item_code

	doc.item_name = payload.get("item_name")
	doc.description = payload.get("description")
	doc.item_group = payload.get("item_group")
	doc.uom = payload.get("uom")
	doc.price = payload.get("price")
	doc.stock_qty = payload.get("stock_qty")
	doc.barcode = payload.get("barcode")
	doc.is_active = payload.get("is_active", 1)
	doc.pushed_at = payload.get("pushed_at")
	doc.last_synced = frappe.utils.now_datetime()

	doc.save(ignore_permissions=True)
	return {"item_code": item_code, "status": "success", "name": doc.name}


@frappe.whitelist(allow_guest=True)
def sync_item(**payload):
	franchise = get_authenticated_franchise()
	result = _upsert_item(franchise, payload)
	frappe.db.commit()
	return result


@frappe.whitelist(allow_guest=True)
def bulk_sync_items():
	franchise = get_authenticated_franchise()
	items = frappe.local.form_dict.get("items") or []
	results = [_upsert_item(franchise, item) for item in items]
	frappe.db.commit()
	return {
		"total": len(results),
		"success": len([r for r in results if r["status"] == "success"]),
		"skipped": len([r for r in results if r["status"] == "skipped"]),
		"error": len([r for r in results if r["status"] == "error"]),
		"results": results,
	}


@frappe.whitelist(allow_guest=True)
@rate_limit(key="get_nearby_items", limit=60, seconds=60)
def get_nearby_items(latitude, longitude, category=None, item_group=None, q=None, limit=50, offset=0):
	latitude = float(latitude)
	longitude = float(longitude)
	limit = min(int(limit), 100)
	offset = int(offset)

	conditions = ["item.is_active = 1", "franchise.status = 'Active'"]
	values = {"lat": latitude, "lng": longitude, "limit": limit, "offset": offset}

	if category:
		conditions.append("item.category = %(category)s")
		values["category"] = category

	if item_group:
		conditions.append("item.item_group = %(item_group)s")
		values["item_group"] = item_group

	if q:
		conditions.append("item.item_name LIKE %(q)s")
		values["q"] = f"%{q}%"

	where_clause = " AND ".join(conditions)

	rows = frappe.db.sql(
		f"""
		SELECT
			item.name, item.item_code, item.item_name, item.description,
			item.item_group, item.category, item.uom, item.price, item.stock_qty,
			item.image, item.barcode, item.franchise,
			franchise.franchise_name, franchise.city, franchise.pincode,
			franchise.serviceable_radius_km,
			item.latitude, item.longitude,
			(6371 * ACOS(
				LEAST(1, GREATEST(-1,
					COS(RADIANS(%(lat)s)) * COS(RADIANS(item.latitude)) *
					COS(RADIANS(item.longitude) - RADIANS(%(lng)s)) +
					SIN(RADIANS(%(lat)s)) * SIN(RADIANS(item.latitude))
				))
			)) AS distance_km
		FROM `tabItem` item
		JOIN `tabFranchise` franchise ON franchise.name = item.franchise
		WHERE {where_clause}
		HAVING distance_km <= franchise.serviceable_radius_km
		ORDER BY distance_km ASC
		LIMIT %(limit)s OFFSET %(offset)s
		""",
		values,
		as_dict=True,
	)

	for row in rows:
		row["image"] = frappe.utils.get_url(row["image"]) if row["image"] else None
		row["distance_km"] = round(row["distance_km"], 2)

	return {"count": len(rows), "items": rows}
