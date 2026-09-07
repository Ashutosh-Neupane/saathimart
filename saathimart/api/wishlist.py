"""
Wishlist API — toggle products in/out of the current user's wishlist.
Requires login.
"""
import frappe
from frappe import _
from saathimart.api.responses import handle_api_errors
from saathimart.api.get_commit_guard import commit_on_get


@frappe.whitelist()
@handle_api_errors
def get_wishlist():
    """Return list of product slugs in current user's wishlist."""
    if frappe.session.user == "Guest":
        return []

    items = frappe.get_list(
        "Wishlist",
        filters={"user": frappe.session.user},
        fields=["product"],
    )
    slugs = []
    for item in items:
        slug = frappe.db.get_value("Product", item.product, "slug")
        if slug:
            slugs.append(slug)
    return slugs


@frappe.whitelist()
@handle_api_errors
@commit_on_get
def toggle_wishlist(product_slug):
    """Toggle product in wishlist. Returns updated slug list."""
    if frappe.session.user == "Guest":
        frappe.throw(_("Please login to manage wishlist"), frappe.PermissionError)

    product_name = frappe.db.get_value("Product", {"slug": product_slug}, "name")
    if not product_name:
        frappe.throw(_("Product not found"), frappe.DoesNotExistError)

    existing = frappe.db.get_value(
        "Wishlist",
        {"user": frappe.session.user, "product": product_name},
    )

    if existing:
        frappe.db.delete("Wishlist", existing)
    else:
        # new_doc takes the doctype NAME (a string), not a dict — passing a
        # dict raised "unhashable type: dict" on every add-to-wishlist.
        doc = frappe.new_doc("Wishlist")
        doc.user = frappe.session.user
        doc.product = product_name
        doc.created_at = frappe.utils.now_datetime()
        doc.insert(ignore_permissions=True)

    return get_wishlist()
