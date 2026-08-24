"""
Review API — product reviews and ratings.

Backed by SM Product Review. avg_rating/review_count on Saathi Item are
denormalized (recomputed by SM Product Review's own on_update/on_trash
hooks — see that doctype's .py) so list_products/get_product
(api/catalog.py) can read them without an extra aggregate query per item.

"product_slug" here is a Saathi Item's docname (its "<franchise>-<item_code>"
name) — same convention as api/catalog.py, since this middleware has no
separate cross-franchise product identity for a review to hang off of
independent of which franchise's listing it's for.
"""
import frappe

from saathi_middleware.api.responses import handle_api_errors
from frappe import _
from frappe.utils import flt, cint


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def get_product_rating(product_slug):
    """Quick rating summary for a product (product cards, PDP header)."""
    row = frappe.db.get_value(
        "Saathi Item", product_slug, ["avg_rating", "review_count"], as_dict=True
    )
    if not row:
        return {"avg_rating": 0, "review_count": 0}
    return {"avg_rating": flt(row.avg_rating), "review_count": cint(row.review_count)}


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def list_reviews(product_slug, page=1, page_size=10):
    """List approved reviews for a product, paginated, newest first."""
    if not frappe.db.exists("Saathi Item", product_slug):
        frappe.throw(_("Product not found"), frappe.DoesNotExistError)

    page = max(cint(page), 1)
    page_size = min(max(cint(page_size), 1), 50)

    # get_all, not get_list: this doctype's role permissions are
    # customer/admin-scoped for moderation from the desk (see
    # has_review_permission), but approved reviews are meant to be
    # publicly readable by guests — the explicit status=Approved filter
    # below is the actual security boundary, same pattern as
    # order.get_payment_modes.
    reviews = frappe.get_all(
        "SM Product Review",
        filters={"item": product_slug, "status": "Approved"},
        fields=["name", "reviewer_name", "rating", "comment", "creation"],
        order_by="creation desc",
        limit_start=(page - 1) * page_size,
        limit_page_length=page_size,
    )
    total = frappe.db.count("SM Product Review", {"item": product_slug, "status": "Approved"})

    return {"product": product_slug, "reviews": reviews, "page": page, "page_size": page_size, "total": total}


@frappe.whitelist()
@handle_api_errors
def add_review(product_slug, rating, comment=""):
    """
    Add or update the logged-in user's review for a product. One review
    per user per product — re-submitting edits the existing one and
    resets it to Pending (an edited review needs re-approval same as a
    new one).
    """
    if frappe.session.user == "Guest":
        frappe.throw(_("Please login to write a review"), frappe.PermissionError)

    if not frappe.db.exists("Saathi Item", product_slug):
        frappe.throw(_("Product not found"), frappe.DoesNotExistError)

    rating = cint(rating)
    if rating < 1 or rating > 5:
        frappe.throw(_("Rating must be between 1 and 5"))

    reviewer_name = frappe.db.get_value("User", frappe.session.user, "full_name") or frappe.session.user

    existing = frappe.db.get_value(
        "SM Product Review", {"item": product_slug, "user": frappe.session.user}, "name"
    )
    if existing:
        doc = frappe.get_doc("SM Product Review", existing)
        doc.rating = rating
        doc.comment = comment or doc.comment
        doc.status = "Pending"
        doc.save(ignore_permissions=True)
        return {"message": _("Review updated"), "review_id": doc.name}

    doc = frappe.get_doc({
        "doctype": "SM Product Review",
        "item": product_slug,
        "user": frappe.session.user,
        "reviewer_name": reviewer_name,
        "rating": rating,
        "comment": comment or "",
        "status": "Pending",
    })
    doc.insert(ignore_permissions=True)
    frappe.db.commit()
    return {"message": _("Review submitted for approval"), "review_id": doc.name}
