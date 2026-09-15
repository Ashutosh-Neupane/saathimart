def execute():
	"""Normalize Membership Benefit.brand values to Brand links.

	brand changed from free-text Data to a Link to Brand. Existing rows whose
	value doesn't exactly match a Brand.brand_name would show as broken links,
	so case/whitespace-normalize them against the Brand table first. Rows
	whose value matches no Brand are cleared (a dangling Data value is worse
	than an empty one on a Link field).
	"""
	import frappe

	if not frappe.db.table_exists("Membership Benefit"):
		return

	brands = {b.casefold().strip(): name for b, name in
		frappe.get_all("Brand", fields=["name", "brand_name"], as_list=True)}
	# brand_name == name on this doctype (autoname field:brand_name), so the
	# dict keys cover both spellings already.

	for name, brand in frappe.get_all(
		"Membership Benefit", fields=["name", "brand"], as_list=True
	):
		if not brand:
			continue
		matched = brands.get(brand.casefold().strip())
		if matched != brand:
			frappe.db.set_value("Membership Benefit", name, "brand", matched or None)
