"""
GET-mutation commit guard.

Frappe only commits the request transaction for "unsafe" HTTP methods
(POST/PUT/DELETE/PATCH — see frappe.app: UNSAFE_HTTP_METHODS). A whitelisted
endpoint that mutates data but is called over GET (the storefront's
add_to_cart / update_cart_item / toggle_wishlist calls are GETs) gets HTTP 200
with its writes silently rolled back after the response.

decorator commits the request transaction before returning whenever the
request is a SAFE method. Usage:

    @frappe.whitelist(allow_guest=True)
    @handle_api_errors
    @commit_on_get
    def add_to_cart(...): ...
"""
import frappe

SAFE_HTTP_METHODS = frozenset(("GET", "HEAD", "OPTIONS"))


def commit_on_get(fn):
    """Commit the request transaction when the endpoint was hit via GET/HEAD."""

    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        result = fn(*args, **kwargs)
        try:
            request = frappe.local.request
            if request is not None and request.method in SAFE_HTTP_METHODS:
                frappe.db.commit()
        except Exception:
            # Never mask the endpoint result because of a commit hiccup —
            # but surface it, since silently uncommitted writes are the bug
            # this guard exists to prevent.
            frappe.log_error(frappe.get_traceback(), "commit_on_get failed")
        return result

    return wrapper
