"""
Backfills slug/description on the seeded Saathi Payment Modes with exactly
the values saathimart-fe hardcoded in lib/types/checkout.ts, so moving the
checkout method list onto the CMS is visually a no-op.

`logo` is deliberately left empty: the storefront keeps bundled artwork per
slug (/images/checkout/esewa.svg etc.) and only uses an uploaded logo when
one exists — same fallback pattern SM Banner already uses for hero art.

Note "card" → "Bank Transfer": the frontend showed it as "Credit Card /
Debit Card", but the backing mode is offline/manual reconciliation
(is_online = 0), because payments.py routes ANY is_online mode through
eSewa. The display name stays as-is to avoid changing what shoppers see;
the slug is what the storefront actually keys on.

Idempotent — only fills a slug that is missing, never overwrites an admin's.
"""
import frappe

_DISPLAY = {
	"eSewa": {
		"slug": "esewa",
		"description": "Pay from e - sewa wallet.",
	},
	"Bank Transfer": {
		"slug": "card",
		"description": "Use Card to complete payment.",
	},
	"COD": {
		"slug": "cash-on-delivery",
		"description": "Pay our rider while delivering your product.",
	},
}


def execute():
	for mode_name, values in _DISPLAY.items():
		if not frappe.db.exists("Saathi Payment Mode", mode_name):
			continue

		doc = frappe.get_doc("Saathi Payment Mode", mode_name)
		changed = False

		if not doc.get("slug"):
			doc.slug = values["slug"]
			changed = True
		if not doc.get("description"):
			doc.description = values["description"]
			changed = True

		if changed:
			doc.save(ignore_permissions=True)

	frappe.db.commit()
