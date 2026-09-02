"""
Delivery Time Slots API — scheduled delivery windows.

Customers can pick a delivery time window (e.g., "Tomorrow 10:00-12:00")
instead of getting delivery "sometime today". This is essential for
grocery platforms where customers plan their day around deliveries.

Endpoints:
  - get_available_slots():      List available slots for a given date
  - book_slot():                Reserve a slot for an order
  - cancel_slot_booking():      Release a booked slot
  - get_slot_capacity():        Check how many orders remain in a slot
"""
import frappe
from frappe import _
from frappe.utils import cint, getdate, nowdate, add_days, flt
from saathimart.api.responses import handle_api_errors


# ── Helpers ────────────────────────────────────────────────────────────────

def _get_weekday_name(date_str):
    """Return the weekday name for a date string (e.g., 'Monday')."""
    from datetime import datetime
    dt = getdate(date_str)
    return dt.strftime("%A")


def _is_slot_available_for_date(slot, target_date):
    """Check if a slot is available for a specific date."""
    if not slot.is_active:
        return False

    # Check day-of-week filter
    weekday = _get_weekday_name(target_date)
    if slot.day_of_week not in ("Every Day", weekday):
        return False

    # Check capacity
    if slot.max_orders and slot.current_orders >= slot.max_orders:
        return False

    return True


# ── API Endpoints ──────────────────────────────────────────────────────────

@frappe.whitelist(allow_guest=True)
@handle_api_errors
def get_available_slots(target_date=None, warehouse=None):
    """Get available delivery time slots for a given date.

    Args:
        target_date: Date string (YYYY-MM-DD). Defaults to tomorrow.
        warehouse: Optional warehouse name to filter slots by scope.

    Returns:
        list of available slots with capacity info
    """
    if not target_date:
        target_date = str(add_days(nowdate(), 1))

    # Validate date is not in the past
    if getdate(target_date) < getdate(nowdate()):
        frappe.throw(_("Cannot book slots for past dates"))

    # Max 7 days in advance
    if getdate(target_date) > getdate(add_days(nowdate(), 7)):
        frappe.throw(_("Can only book up to 7 days in advance"))

    weekday = _get_weekday_name(target_date)

    # Get all active slots
    all_slots = frappe.get_all(
        "Delivery Time Slot",
        filters={"is_active": 1},
        fields=["name", "slot_name", "start_time", "end_time", "day_of_week",
                "max_orders", "current_orders", "delivery_charge_override",
                "warehouse_scope"],
        order_by="start_time asc",
    )

    available = []
    for slot in all_slots:
        # Filter by day of week
        if slot.day_of_week not in ("Every Day", weekday):
            continue

        # Filter by warehouse scope
        if warehouse and slot.warehouse_scope:
            scoped_warehouses = [w.strip() for w in slot.warehouse_scope.split(",")]
            if warehouse not in scoped_warehouses:
                continue

        # Calculate remaining capacity
        max_orders = cint(slot.max_orders) or 50
        current = cint(slot.current_orders) or 0
        remaining = max_orders - current

        # Count actual bookings for this date (more accurate than current_orders)
        booked = frappe.db.count(
            "Delivery Slot Booking",
            {"slot": slot.name, "delivery_date": target_date, "status": ["in", ["Booked", "Confirmed"]]},
        )
        remaining = max_orders - booked

        available.append({
            "slot_id": slot.name,
            "slot_name": slot.slot_name,
            "start_time": str(slot.start_time) if slot.start_time else "",
            "end_time": str(slot.end_time) if slot.end_time else "",
            "remaining_capacity": remaining,
            "is_available": remaining > 0,
            "delivery_charge_override": flt(slot.delivery_charge_override or 0),
            "is_peak": remaining <= 5,  # Last few slots = peak indicator
        })

    return {
        "date": target_date,
        "weekday": weekday,
        "slots": available,
    }


@frappe.whitelist()
@handle_api_errors
def book_slot(slot_id, target_date, order_name=None):
    """Reserve a delivery slot for an order.

    Args:
        slot_id: Delivery Time Slot name
        target_date: Delivery date (YYYY-MM-DD)
        order_name: Optional order name to link

    Returns:
        dict with booking info
    """
    user = frappe.session.user
    if user == "Guest":
        frappe.throw(_("Login required"), frappe.PermissionError)

    # Validate slot exists and is active
    slot = frappe.get_doc("Delivery Time Slot", slot_id)
    if not slot.is_active:
        frappe.throw(_("This time slot is no longer available"))

    # Validate date
    if getdate(target_date) < getdate(nowdate()):
        frappe.throw(_("Cannot book slots for past dates"))

    if getdate(target_date) > getdate(add_days(nowdate(), 7)):
        frappe.throw(_("Can only book up to 7 days in advance"))

    # Validate day of week
    weekday = _get_weekday_name(target_date)
    if slot.day_of_week not in ("Every Day", weekday):
        frappe.throw(_("Slot '{0}' is not available on {1}").format(slot.slot_name, weekday))

    # Check capacity
    booked = frappe.db.count(
        "Delivery Slot Booking",
        {"slot": slot_id, "delivery_date": target_date, "status": ["in", ["Booked", "Confirmed"]]},
    )
    max_orders = cint(slot.max_orders) or 50
    if booked >= max_orders:
        frappe.throw(_("This time slot is fully booked"))

    # Create booking
    booking = frappe.new_doc("Delivery Slot Booking")
    booking.slot = slot_id
    booking.delivery_date = target_date
    booking.order = order_name
    booking.status = "Booked"
    booking.insert(ignore_permissions=True)
    frappe.db.commit()

    return {
        "ok": True,
        "booking_id": booking.name,
        "slot_name": slot.slot_name,
        "delivery_date": target_date,
        "start_time": str(slot.start_time) if slot.start_time else "",
        "end_time": str(slot.end_time) if slot.end_time else "",
        "remaining_capacity": max_orders - booked - 1,
    }


@frappe.whitelist()
@handle_api_errors
def cancel_slot_booking(booking_id):
    """Cancel a slot booking and release the capacity.

    Args:
        booking_id: Delivery Slot Booking name

    Returns:
        dict with status
    """
    user = frappe.session.user
    if user == "Guest":
        frappe.throw(_("Login required"), frappe.PermissionError)

    booking = frappe.get_doc("Delivery Slot Booking", booking_id)
    booking.status = "Cancelled"
    booking.save(ignore_permissions=True)
    frappe.db.commit()

    return {"ok": True, "booking_id": booking_id}


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def get_slot_capacity(target_date=None):
    """Get capacity overview for all slots on a date.

    Returns summary of how many slots are available, almost full, and full.
    """
    if not target_date:
        target_date = str(add_days(nowdate(), 1))

    slots = get_available_slots(target_date=target_date)
    slot_list = slots.get("slots", [])

    available = sum(1 for s in slot_list if s["is_available"] and not s["is_peak"])
    almost_full = sum(1 for s in slot_list if s["is_peak"])
    full = sum(1 for s in slot_list if not s["is_available"])

    return {
        "date": target_date,
        "total_slots": len(slot_list),
        "available": available,
        "almost_full": almost_full,
        "full": full,
        "recommendation": (
            "Good availability" if available > 2
            else "Book soon — limited slots left" if available > 0
            else "No slots available — try another date"
        ),
    }
