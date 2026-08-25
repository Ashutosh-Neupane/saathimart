import frappe


def execute():
	"""
	Product.brand changed from a free-text Data field to a Link -> Brand
	(mirroring saathi_middleware's Saathi Item -> SM Brand). Any legacy free-
	text value is promoted to a real Brand document so no product loses its
	brand after the fieldtype switch. Matching is case-insensitive ("nestle"
	and "Nestle" collapse into one Brand) and every Product is repointed at
	the canonical Brand name.
	"""
	existing = {b.name.lower(): b.name for b in frappe.get_all("Brand", fields=["name"])}

	rows = frappe.get_all(
		"Product",
		filters={"brand": ["is", "set"]},
		fields=["name", "brand"],
	)

	for row in rows:
		text = (row.brand or "").strip()
		if not text:
			continue

		key = text.lower()
		canonical = existing.get(key)
		if not canonical:
			doc = frappe.get_doc({
				"doctype": "Brand",
				"brand_name": text,
			})
			# insert directly — ignore_permissions so the patch works even if
			# it is executed as a restricted user during migration.
			doc.flags.ignore_permissions = True
			doc.insert()
			existing[key] = doc.name
			canonical = doc.name

		if row.brand != canonical:
			frappe.db.set_value("Product", row.name, "brand", canonical, update_modified=False)
