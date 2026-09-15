def execute():
	"""Drop Vendor.default_warehouse and clear orphaned Order Item.vendor_listing values.

	1. Vendor.default_warehouse was a Link to ERPNext's Warehouse doctype,
	   which does not exist on the no-ERPNext hub - opening a Vendor form
	   whose value was set broke link resolution. Warehouses live only in
	   the Vendor Warehouse child table (that is what get_default_warehouse
	   and every stock row actually use).
	2. Order Item.vendor_listing rows pointing at Vendor Listings that no
	   longer exist (listings wiped in a catalogue reseed while their
	   orders remained) are cleared so link integrity holds.
	"""
	import frappe

	# 1. drop the dead column
	col = frappe.db.sql(
		"SELECT COUNT(*) FROM information_schema.columns "
		"WHERE table_schema = DATABASE() AND table_name = 'tabVendor' "
		"AND column_name = 'default_warehouse'"
	)[0][0]
	if col:
		frappe.db.sql_ddl("ALTER TABLE `tabVendor` DROP COLUMN `default_warehouse`")

	# 2. clear orphaned vendor_listing refs on order items
	frappe.db.sql("""
		UPDATE `tabOrder Item` oi
		LEFT JOIN `tabVendor Listing` vl ON vl.name = oi.vendor_listing
		SET oi.vendor_listing = ''
		WHERE oi.vendor_listing IS NOT NULL AND oi.vendor_listing != ''
		  AND vl.name IS NULL
	""")
