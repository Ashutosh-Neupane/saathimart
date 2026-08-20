"""
Wishlist API — saved-for-later products for logged-in customers.

Matches the frontend's lib/api/index.ts contract: getWishlist() returns
plain product ids (Saathi Item names), toggleWishlist(product_slug) flips
membership and returns the updated id list. There's no guest-side wishlist
to merge on login (hooks/use-wishlist.tsx blocks wishlist actions entirely
until isLoggedIn) — login "syncing" is just this GET firing once the
frontend's auth state flips, no separate merge endpoint needed.
"""
import frappe

from saathi_middleware.api.responses import handle_api_errors, raw
from frappe import _
from frappe.utils import now_datetime


def _require_login():
	if frappe.session.user == "Guest":
		frappe.throw(_("Please login to manage your wishlist"), frappe.PermissionError)


@frappe.whitelist()
@handle_api_errors
def get_wishlist():
	_require_login()
	return frappe.get_list(
		"SM Wishlist Item",
		filters={"user": frappe.session.user},
		pluck="item",
		order_by="added_at desc",
	)


@frappe.whitelist()
@handle_api_errors
def toggle_wishlist(product_slug):
	_require_login()
	if not product_slug:
		frappe.throw(_("product_slug is required"))

	existing = frappe.db.exists(
		"SM Wishlist Item", {"user": frappe.session.user, "item": product_slug}
	)
	if existing:
		frappe.delete_doc("SM Wishlist Item", existing, ignore_permissions=True)
	else:
		if not frappe.db.exists("Saathi Item", product_slug):
			frappe.throw(_("Product not found"), frappe.DoesNotExistError)
		frappe.get_doc({
			"doctype": "SM Wishlist Item",
			"user": frappe.session.user,
			"item": product_slug,
			"added_at": now_datetime(),
		}).insert(ignore_permissions=True)

	frappe.db.commit()
	return raw(get_wishlist)()
