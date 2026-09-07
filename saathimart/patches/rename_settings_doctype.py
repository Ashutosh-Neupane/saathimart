def execute():
	"""Rename the app's `Settings` single doctype to `SaathiMart Settings`.

	Registered under [pre_model_sync]: it must run *before* Frappe syncs the
	renamed JSON files, so the old doctype row (and its single values) are
	renamed in place rather than leaving a stale `Settings` doctype next to a
	fresh `SaathiMart Settings` with empty values.

	Single-doctype data lives in `tabSingles` keyed by doctype name; field
	definitions live in `tabDocField` keyed by parent, so both are updated.
	"""
	import frappe

	# Guard: only act if the app-level `Settings` doctype exists (module
	# Saathimart distinguishes it from any core doctype) and the new name
	# doesn't already exist.
	if not frappe.db.exists(
		"DocType", "Settings", cache=True
	) or frappe.db.sql(
		"SELECT 1 FROM `tabDocType` WHERE name = 'SaathiMart Settings'"
	):
		return

	frappe.db.sql(
		"UPDATE `tabDocType` SET name = 'SaathiMart Settings' WHERE name = 'Settings' AND module = 'Saathimart'"
	)
	frappe.db.sql(
		"UPDATE `tabSingles` SET doctype = 'SaathiMart Settings' WHERE doctype = 'Settings'"
	)
	frappe.db.sql(
		"UPDATE `tabDocField` SET parent = 'SaathiMart Settings' WHERE parent = 'Settings'"
	)
	frappe.db.sql(
		"UPDATE `tabDocPerm` SET parent = 'SaathiMart Settings' WHERE parent = 'Settings'"
	)
	frappe.db.sql(
		"UPDATE `tabCustom DocPerm` SET parent = 'SaathiMart Settings' WHERE parent = 'Settings'"
	)
	frappe.db.sql(
		"UPDATE `tabCustom Field` SET dt = 'SaathiMart Settings' WHERE dt = 'Settings'"
	)
	frappe.db.sql(
		"UPDATE `tabProperty Setter` SET doc_type = 'SaathiMart Settings' WHERE doc_type = 'Settings'"
	)

	# Single-doctype rows keep their identity in tabSingles: the `name`
	# field's *value* must also become the new doctype name, or
	# frappe.get_single() returns a doc whose .name is still "Settings".
	frappe.db.sql(
		"UPDATE `tabSingles` SET value = 'SaathiMart Settings'"
		" WHERE doctype = 'SaathiMart Settings' AND field = 'name' AND value = 'Settings'"
	)

	# Encrypted password fields live in __Auth keyed by doctype.
	frappe.db.sql(
		"UPDATE `__Auth` SET doctype = 'SaathiMart Settings' WHERE doctype = 'Settings'"
	)

	frappe.clear_cache()
	frappe.db.commit()
