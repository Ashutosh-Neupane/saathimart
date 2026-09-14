def execute():
	"""Drop Vendor Listing's duplicated available/reserved/physical qty columns.

	Vendor Stock is the single source of truth for quantities. The Vendor
	Listing copies were display-only caches that drifted from truth (7 of 442
	rows had drifted in production) and one reader (location.nearest_vendor_
	for_product) was actually serving the stale numbers to customers. Every
	reader now joins Vendor Stock directly, so the columns are dead weight and
	a correctness hazard.
	"""
	import frappe

	existing = {
		r[0] for r in frappe.db.sql(
			"SELECT column_name FROM information_schema.columns "
			"WHERE table_schema = DATABASE() AND table_name = 'tabVendor Listing' "
			"AND column_name IN ('available_qty', 'reserved_qty', 'physical_qty')"
		)
	}
	for col in ("available_qty", "reserved_qty", "physical_qty"):
		if col in existing:
			frappe.db.sql_ddl(f"ALTER TABLE `tabVendor Listing` DROP COLUMN `{col}`")
