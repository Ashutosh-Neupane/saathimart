"""
Background tasks for Redis Stream publishing.

These tasks are executed by Frappe's RQ workers (not HTTP workers),
ensuring fast response times for API calls.

Usage via frappe.enqueue():
    frappe.enqueue(
        "saathimart.streams.tasks.publish_event",
        vendor_id="VENDOR001",
        event_type="order.new",
        payload={"order_id": "ORD-001"},
        queue="short",
        enqueue_after_commit=True
    )
"""
import frappe
import json
from frappe import _
from typing import Dict, Any, List
from saathimart.streams.publisher import StreamPublisher


def publish_event(vendor_id: str, event_type: str, payload: Dict[str, Any]) -> str:
    """
    Background task to publish a single event to vendor's stream.
    
    This is the recommended way to publish events - it runs in the
    background worker, not the web worker, ensuring fast API responses.
    
    Args:
        vendor_id: Target vendor ID
        event_type: Event type (e.g., "order.new")
        payload: Event data (must be JSON-serializable)
    
    Returns:
        Redis Stream message ID
    
    Example:
        >>> frappe.enqueue(
        ...     "saathimart.streams.tasks.publish_event",
        ...     vendor_id="VENDOR001",
        ...     event_type="order.new",
        ...     payload={"order_id": "ORD-001"},
        ...     queue="short"
        ... )
    """
    publisher = StreamPublisher(vendor_id)
    return publisher.publish(event_type, payload)


def publish_event_batch(vendor_id: str, events: List[tuple]) -> List[str]:
    """
    Background task to publish multiple events in a pipeline.
    
    More efficient than individual publish_event calls when you have
    many events to send to the same vendor.
    
    Args:
        vendor_id: Target vendor ID
        events: List of (event_type, payload) tuples
    
    Returns:
        List of message IDs
    """
    publisher = StreamPublisher(vendor_id)
    return publisher.publish_batch(events)


def publish_order_created_task(order_name: str) -> Dict[str, Any]:
    """
    Background task to publish order.new event.
    
    Triggered by Order.after_insert() or Order.on_update().
    Runs in background worker to avoid blocking the request.
    
    Args:
        order_name: Order document name
    
    Returns:
        Dict with published message IDs
    """
    order = frappe.get_doc("Order", order_name)
    
    results = {}
    publisher = StreamPublisher(order.vendor) if order.vendor else None
    
    # Multi-vendor order - publish to each vendor
    if not publisher and order.vendor_fulfillments:
        for fulfillment in order.vendor_fulfillments:
            pub = StreamPublisher(fulfillment.vendor)
            msg_id = pub.publish("order.new", _build_order_payload(order, fulfillment.vendor))
            results[fulfillment.vendor] = msg_id
        return results
    
    # Single-vendor order
    if publisher:
        msg_id = publisher.publish("order.new", _build_order_payload(order))
        results[order.vendor] = msg_id
    
    return results


def publish_payment_received_task(order_name: str, amount: float, gateway: str) -> Dict[str, Any]:
    """
    Background task to publish payment.received event.
    
    Triggered by payment gateway callback or manual payment confirmation.
    
    Args:
        order_name: Order document name
        amount: Payment amount
        gateway: Payment gateway (e.g., "eSewa", "COD")
    
    Returns:
        Dict with published message IDs
    """
    order = frappe.get_doc("Order", order_name)
    
    results = {}
    
    # Multi-vendor
    if not order.vendor and order.vendor_fulfillments:
        for fulfillment in order.vendor_fulfillments:
            pub = StreamPublisher(fulfillment.vendor)
            msg_id = pub.publish("payment.received", {
                "order_id": order_name,
                "amount": amount,
                "gateway": gateway,
                "vendor_id": fulfillment.vendor,
            })
            results[fulfillment.vendor] = msg_id
        return results
    
    # Single-vendor
    if order.vendor:
        pub = StreamPublisher(order.vendor)
        msg_id = pub.publish("payment.received", {
            "order_id": order_name,
            "amount": amount,
            "gateway": gateway,
        })
        results[order.vendor] = msg_id
    
    return results


def publish_stock_update_task(vendor_id: str, product_id: str, qty_change: float, 
                               warehouse: str = None) -> str:
    """
    Background task to publish stock.update event.
    
    Triggered by stock receipt, deduction, or adjustment.
    
    Args:
        vendor_id: Vendor ID
        product_id: Product ID
        qty_change: Quantity change (+ or -)
        warehouse: Optional warehouse ID
    
    Returns:
        Message ID
    """
    publisher = StreamPublisher(vendor_id)
    return publisher.publish("stock.update", {
        "product_id": product_id,
        "qty_change": qty_change,
        "warehouse": warehouse,
        "timestamp": frappe.utils.now_datetime().isoformat(),
    })


def publish_price_update_task(vendor_id: str, product_id: str, old_price: float, 
                               new_price: float) -> str:
    """
    Background task to publish price.update event.
    
    Triggered by vendor price change.
    
    Args:
        vendor_id: Vendor ID
        product_id: Product ID
        old_price: Previous price
        new_price: New price
    
    Returns:
        Message ID
    """
    publisher = StreamPublisher(vendor_id)
    return publisher.publish("price.update", {
        "product_id": product_id,
        "old_price": old_price,
        "new_price": new_price,
        "timestamp": frappe.utils.now_datetime().isoformat(),
    })


def _build_order_payload(order, vendor_filter: str = None) -> Dict[str, Any]:
    """Build order payload for order.new event."""
    items = [
        {
            "product": item.product,
            "product_name": item.product_name,
            "qty": item.qty,
            "rate": item.rate,
        }
        for item in order.items
        if not vendor_filter or item.vendor == vendor_filter
    ]
    
    return {
        "order_id": order.name,
        "customer_name": order.customer_name,
        "customer_phone": order.customer_phone,
        "customer_email": order.customer_email,
        "delivery_address": order.delivery_address,
        "delivery_zone": order.delivery_zone,
        "items": items,
        "grand_total": order.grand_total,
        "payment_method": order.payment_method,
        "payment_status": order.payment_status,
        "created_at": order.creation.isoformat(),
    }
