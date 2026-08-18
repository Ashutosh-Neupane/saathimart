"""
Seeds the real Saathi Payment Mode records matching what the frontend's
PAYMENT_METHOD_TO_BACKEND (lib/types/checkout.ts) actually sends: "eSewa",
"Bank Transfer", "COD". Idempotent — updates rather than duplicates if a
site admin already has these.

Bank Transfer is deliberately is_online=0 (offline/manual reconciliation,
same as COD) — payments.py's initiate_payment() unconditionally routes
ANY is_online=1 mode through eSewa's flow, so there's no way to mark a
second mode online without it actually being eSewa under the hood. Khalti
was dropped from the frontend entirely for the same reason (see
lib/types/checkout.ts) rather than seeded here as a broken online mode.
"""
import frappe


def execute():
	modes = [
		{"mode_name": "eSewa", "is_enabled": 1, "is_online": 1, "display_order": 1},
		{"mode_name": "Bank Transfer", "is_enabled": 1, "is_online": 0, "display_order": 2},
		{"mode_name": "COD", "is_enabled": 1, "is_online": 0, "display_order": 3},
	]

	for m in modes:
		if frappe.db.exists("Saathi Payment Mode", m["mode_name"]):
			doc = frappe.get_doc("Saathi Payment Mode", m["mode_name"])
			doc.is_enabled = m["is_enabled"]
			doc.is_online = m["is_online"]
			doc.display_order = m["display_order"]
			doc.save(ignore_permissions=True)
		else:
			frappe.get_doc({"doctype": "Saathi Payment Mode", **m}).insert(ignore_permissions=True)

	frappe.db.commit()
