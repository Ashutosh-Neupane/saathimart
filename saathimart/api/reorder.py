"""
Reorder / Repeat Order API — one-tap reorder from previous orders.

60% of grocery revenue is repeat purchases. This module lets customers
reorder their previous orders with a single tap, dramatically reducing
friction for the most common use case.

Endpoints:
  - get_reorderable_orders():  List past orders that can be reordered
  - reorder_from_order():      Create a new cart from a past order's items
  - get_quick_reorder():       One-tap reorder of the most recent order
"""
import frappe
from frappe import _
from frappe.utils import flt, now_datetime
from saathimart.api.responses import handle_api_errors


# ── API Endpoints ──────────────────────────────────────────────────────────

@frappe.whitelist()
@handle_api_errors
def get_reorderable_orders(limit=10):
    """Get past orders that can be reordered.

    Returns orders from the last 90 days that were delivered or confirmed,
    sorted by most recent first.

    Args:
        limit: Max orders to return (default 10)

    Returns:
        list of order summaries with reorder info
    """
    user = frappe.session.user
    if user == "Guest":
        frappe.throw(_("Login required"), frappe.PermissionError)

    limit = min(cint(limit) or 10, 20)

    orders = frappe.get_all(
        "Order",
        filters={
            "customer_email": user,
            "status": ["in", ["Delivered", "Confirmed"]],
        },
        fields=["name", "customer_name", "grand_total", "status", "creation",
                "vendor", "payment_method"],
        order_by="creation desc",
        limit_page_length=limit,
    )

    result = []
    for order in orders:
        # Get items for this order
        items = frappe.get_all(
            "Order Item",
            filters={"parent": order.name},
            fields=["product", "product_name", "qty", "rate", "amount", "vendor"],
        )

        # Check which products are still available
        available_items = []
        unavailable_items = []
        for item in items:
            # Check if product is still active
            product_active = frappe.db.get_value(
                "Product", item.product, "status"
            ) == "Active" if frappe.db.exists("Product", item.product) else False

            # Check if vendor listing still exists
            listing_exists = frappe.db.exists(
                "Vendor Listing",
                {"product": item.product, "vendor": item.vendor, "status": "Active"}
            )

            if product_active and listing_exists:
                available_items.append({
                    "product": item.product,
                    "product_name": item.product_name,
                    "qty": item.qty,
                    "rate": flt(item.rate),
                    "amount": flt(item.amount),
                })
            else:
                unavailable_items.append({
                    "product": item.product,
                    "product_name": item.product_name,
                    "qty": item.qty,
                })

        # Calculate reorder total
        reorder_total = sum(i["rate"] * i["qty"] for i in available_items)

        result.append({
            "order_id": order.name,
            "customer_name": order.customer_name,
            "grand_total": flt(order.grand_total),
            "status": order.status,
            "creation": str(order.creation),
            "vendor": order.vendor,
            "payment_method": order.payment_method,
            "available_items": available_items,
            "unavailable_items": unavailable_items,
            "reorder_total": reorder_total,
            "can_reorder": len(available_items) > 0,
            "item_count": len(items),
            "available_count": len(available_items),
        })

    return result


@frappe.whitelist()
@handle_api_errors
def reorder_from_order(order_id):
    """Create a new cart from a past order's items.

    This is the "Reorder" button action — copies all available items
    from a past order into the current cart.

    Args:
        order_id: The order to reorder from

    Returns:
        dict with cart info and any unavailable items
    """
    user = frappe.session.user
    if user == "Guest":
        frappe.throw(_("Login required"), frappe.PermissionError)

    # Verify the order belongs to this user
    order = frappe.get_doc("Order", order_id)
    if order.customer_email != user:
        frappe.throw(_("Not your order"), frappe.PermissionError)

    # Get items from the original order
    items = frappe.get_all(
        "Order Item",
        filters={"parent": order_id},
        fields=["product", "product_name", "qty", "rate", "vendor"],
    )

    added = []
    skipped = []

    for item in items:
        # Check if product is still available
        product_active = frappe.db.get_value(
            "Product", item.product, "status"
        ) == "Active" if frappe.db.exists("Product", item.product) else False

        listing_exists = frappe.db.exists(
            "Vendor Listing",
            {"product": item.product, "vendor": item.vendor, "status": "Active"}
        )

        if product_active and listing_exists:
            # Get current price from vendor listing
            current_price = frappe.db.get_value(
                "Vendor Listing",
                {"product": item.product, "vendor": item.vendor, "status": "Active"},
                "price"
            )

            added.append({
                "product": item.product,
                "product_name": item.product_name,
                "qty": item.qty,
                "original_rate": flt(item.rate),
                "current_rate": flt(current_price or item.rate),
                "vendor": item.vendor,
            })
        else:
            skipped.append({
                "product": item.product,
                "product_name": item.product_name,
                "qty": item.qty,
                "reason": "out_of_stock" if not product_active else "vendor_unavailable",
            })

    # Add to cart
    if added:
        from saathimart.api.cart import add_to_cart
        for item in added:
            try:
                add_to_cart(
                    product=item["product"],
                    qty=item["qty"],
                    vendor=item["vendor"],
                )
            except Exception:
                # If add_to_cart fails, skip this item
                skipped.append({
                    "product": item["product"],
                    "product_name": item["product_name"],
                    "qty": item["qty"],
                    "reason": "add_failed",
                })

    return {
        "ok": True,
        "reorder_from": order_id,
        "added": len(added),
        "skipped": len(skipped),
        "skipped_items": skipped,
        "message": f"Added {len(added)} items to cart" + (
            f", {len(skipped)} unavailable" if skipped else ""
        ),
    }


@frappe.whitelist()
@handle_api_errors
def get_quick_reorder():
    """One-tap reorder of the most recent delivered order.

    Returns the most recent order that can be reordered, with a flag
    indicating if it can be reordered in one tap.

    Returns:
        dict with most recent reorderable order info
    """
    user = frappe.session.user
    if user == "Guest":
        frappe.throw(_("Login required"), frappe.PermissionError)

    # Get most recent delivered/confirmed order
    order = frappe.get_all(
        "Order",
        filters={
            "customer_email": user,
            "status": ["in", ["Delivered", "Confirmed"]],
        },
        fields=["name", "grand_total", "status", "creation"],
        order_by="creation desc",
        limit_page_length=1,
    )

    if not order:
        return {"has_order": False, "message": "No past orders found"}

    # Get items
    items = frappe.get_all(
        "Order Item",
        filters={"parent": order[0].name},
        fields=["product", "product_name", "qty", "rate", "vendor"],
    )

    # Check availability
    available = 0
    for item in items:
        product_active = frappe.db.get_value(
            "Product", item.product, "status"
        ) == "Active" if frappe.db.exists("Product", item.product) else False
        listing_exists = frappe.db.exists(
            "Vendor Listing",
            {"product": item.product, "vendor": item.vendor, "status": "Active"}
        )
        if product_active and listing_exists:
            available += 1

    can_reorder = available > 0
    reorder_total = sum(flt(i.rate) * flt(i.qty) for i in items if can_reorder)

    return {
        "has_order": True,
        "can_reorder": can_reorder,
        "order_id": order[0].name,
        "item_count": len(items),
        "available_count": available,
        "reorder_total": reorder_total,
        "creation": str(order[0].creation),
    }
