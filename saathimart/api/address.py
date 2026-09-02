"""
Address management API — CRUD for customer delivery addresses.
"""
import frappe
from frappe import _
from saathimart.api.responses import handle_api_errors


@frappe.whitelist()
@handle_api_errors
def get_addresses():
    """Get all addresses for the current user."""
    user = frappe.session.user
    if user == "Guest":
        return []

    addresses = frappe.get_all(
        "Address",
        filters={"owner": user},
        fields=["name", "address_title", "address_line1", "address_line2",
                "city", "state", "pincode", "country", "phone", "is_primary_address"],
        order_by="is_primary_address desc, creation desc",
    )
    return addresses


@frappe.whitelist()
@handle_api_errors
def add_address(address_title, address_line1, city, state=None, pincode=None,
                country="Nepal", phone=None, is_primary_address=0):
    """Add a new address for the current user."""
    user = frappe.session.user
    if user == "Guest":
        frappe.throw(_("Login required"), frappe.PermissionError)

    doc = frappe.new_doc("Address")
    doc.address_title = address_title
    doc.address_line1 = address_line1
    doc.address_line2 = None
    doc.city = city
    doc.state = state
    doc.pincode = pincode
    doc.country = country
    doc.phone = phone
    doc.is_primary_address = is_primary_address
    doc.owner = user
    doc.insert(ignore_permissions=True)
    frappe.db.commit()

    return {"ok": True, "address": doc.name}


@frappe.whitelist()
@handle_api_errors
def update_address(address_name, **kwargs):
    """Update an existing address (owner only)."""
    user = frappe.session.user
    if user == "Guest":
        frappe.throw(_("Login required"), frappe.PermissionError)

    doc = frappe.get_doc("Address", address_name)
    if doc.owner != user:
        frappe.throw(_("Not your address"), frappe.PermissionError)

    allowed_fields = ["address_title", "address_line1", "address_line2",
                      "city", "state", "pincode", "country", "phone",
                      "is_primary_address"]
    for field in allowed_fields:
        if field in kwargs and kwargs[field] is not None:
            doc.set(field, kwargs[field])

    doc.save(ignore_permissions=True)
    frappe.db.commit()
    return {"ok": True}


@frappe.whitelist()
@handle_api_errors
def delete_address(address_name):
    """Delete an address (owner only)."""
    user = frappe.session.user
    if user == "Guest":
        frappe.throw(_("Login required"), frappe.PermissionError)

    doc = frappe.get_doc("Address", address_name)
    if doc.owner != user:
        frappe.throw(_("Not your address"), frappe.PermissionError)

    frappe.delete_doc("Address", address_name, ignore_permissions=True)
    frappe.db.commit()
    return {"ok": True}
