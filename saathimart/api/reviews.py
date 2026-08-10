"""
Review API — product reviews and ratings.

Endpoints:
  list_reviews     — GET /api/method/saathimart.api.reviews.list_reviews
  add_review       — POST /api/method/saathimart.api.reviews.add_review
  get_product_rating — GET /api/method/saathimart.api.reviews.get_product_rating
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, now_datetime


@frappe.whitelist(allow_guest=True)
def list_reviews(product_slug, page=1, page_size=10):
    """List approved reviews for a product, paginated."""
    name = frappe.db.get_value("Product", {"slug": product_slug, "status": "Active"}, "name")
    if not name:
        frappe.throw(_("Product not found"), frappe.DoesNotExistError)

    page = max(1, int(page))
    page_size = min(50, max(1, int(page_size)))

    reviews = frappe.get_list(
        "Review",
        filters={"product": name, "status": "Approved"},
        fields=["name", "reviewer_name", "rating", "comment", "creation"],
        order_by="creation desc",
        limit_start=(page - 1) * page_size,
        limit_page_length=page_size,
    )

    stats = frappe.db.sql("""
        SELECT COUNT(*) as count, AVG(rating) as avg
        FROM `tabReview`
        WHERE product = %s AND status = 'Approved'
    """, (name,), as_dict=True)

    return {
        "product": product_slug,
        "reviews": reviews,
        "page": page,
        "page_size": page_size,
        "total": stats[0].count if stats else 0,
        "avg_rating": round(flt(stats[0].avg or 0), 1) if stats else 0,
        "rating_breakdown": _get_rating_breakdown(name),
    }


def _get_rating_breakdown(product_name):
    """Return count of reviews per star rating (1-5)."""
    rows = frappe.db.sql("""
        SELECT rating, COUNT(*) as count
        FROM `tabReview`
        WHERE product = %s AND status = 'Approved'
        GROUP BY rating
        ORDER BY rating DESC
    """, (product_name,), as_dict=True)
    breakdown = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    for r in rows:
        breakdown[int(r.rating)] = r.count
    return breakdown


@frappe.whitelist()
def add_review(product_slug, rating, comment=""):
    """
    Add a product review. Requires login.
    One review per user per product.
    """
    if frappe.session.user == "Guest":
        frappe.throw(_("Please login to write a review"), frappe.PermissionError)

    name = frappe.db.get_value("Product", {"slug": product_slug, "status": "Active"}, "name")
    if not name:
        frappe.throw(_("Product not found"), frappe.DoesNotExistError)

    rating = int(rating)
    if rating < 1 or rating > 5:
        frappe.throw(_("Rating must be between 1 and 5"))

    # Check for existing review by this user
    existing = frappe.db.get_value(
        "Review",
        {"product": name, "user": frappe.session.user},
        "name",
    )
    if existing:
        # Update existing review
        doc = frappe.get_doc("Review", existing)
        doc.rating = rating
        doc.comment = comment or doc.comment
        doc.status = "Pending"
        doc.save(ignore_permissions=True)
        return {"message": _("Review updated"), "review_id": doc.name}

    doc = frappe.new_doc("Review")
    doc.product = name
    doc.user = frappe.session.user
    doc.reviewer_name = frappe.db.get_value("User", frappe.session.user, "full_name") or frappe.session.user
    doc.rating = rating
    doc.comment = comment or ""
    doc.status = "Pending"
    doc.insert(ignore_permissions=True)
    frappe.db.commit()

    return {"message": _("Review submitted for approval"), "review_id": doc.name}


@frappe.whitelist(allow_guest=True)
def get_product_rating(product_slug):
    """Quick rating summary for a product (used in product cards)."""
    name = frappe.db.get_value("Product", {"slug": product_slug, "status": "Active"}, "name")
    if not name:
        return {"avg_rating": 0, "review_count": 0}

    stats = frappe.db.sql("""
        SELECT COUNT(*) as count, AVG(rating) as avg
        FROM `tabReview`
        WHERE product = %s AND status = 'Approved'
    """, (name,), as_dict=True)

    return {
        "product": product_slug,
        "avg_rating": round(flt(stats[0].avg or 0), 1) if stats else 0,
        "review_count": stats[0].count if stats else 0,
    }


def _update_product_rating(doc, method):
    """Hook: update Product.avg_rating and review_count when a review is submitted/cancelled."""
    if not doc.product:
        return
    stats = frappe.db.sql("""
        SELECT COUNT(*) as count, AVG(rating) as avg
        FROM `tabReview`
        WHERE product = %s AND status = 'Approved'
    """, (doc.product,), as_dict=True)
    count = stats[0].count if stats else 0
    avg = flt(stats[0].avg or 0) if stats else 0
    frappe.db.set_value("Product", doc.product, {
        "avg_rating": round(avg, 1),
        "review_count": count,
    })
