"""
Address API — saved delivery addresses for logged-in customers.
"""
import frappe

from saathi_middleware.api.responses import handle_api_errors
from frappe import _


def _require_login():
    if frappe.session.user == "Guest":
        frappe.throw(_("Please login to manage addresses"), frappe.PermissionError)


def _get_owned_address(name):
    doc = frappe.get_doc("SM Address", name)
    if doc.user != frappe.session.user and "SM Admin" not in frappe.get_roles():
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    return doc


@frappe.whitelist()
@handle_api_errors
def list_addresses():
    _require_login()
    return frappe.get_list(
        "SM Address",
        filters={"user": frappe.session.user},
        fields=["name", "label", "full_name", "phone", "city", "street_address",
                "landmark", "lat", "lng", "is_default"],
        order_by="is_default desc, modified desc",
    )


@frappe.whitelist()
@handle_api_errors
def add_address(full_name, phone, city, street_address, label="Home",
                landmark=None, lat=None, lng=None, is_default=0):
    _require_login()
    doc = frappe.new_doc("SM Address")
    doc.user = frappe.session.user
    doc.label = label
    doc.full_name = full_name
    doc.phone = phone
    doc.city = city
    doc.street_address = street_address
    doc.landmark = landmark or ""
    doc.lat = lat
    doc.lng = lng
    doc.is_default = 1 if int(is_default or 0) else 0
    doc.insert(ignore_permissions=True)
    return doc.as_dict()


@frappe.whitelist()
@handle_api_errors
def update_address(name, **kwargs):
    _require_login()
    doc = _get_owned_address(name)
    for field in ("label", "full_name", "phone", "city", "street_address",
                  "landmark", "lat", "lng"):
        if field in kwargs and kwargs[field] is not None:
            doc.set(field, kwargs[field])
    if "is_default" in kwargs:
        doc.is_default = 1 if int(kwargs["is_default"] or 0) else 0
    doc.save(ignore_permissions=True)
    return doc.as_dict()


@frappe.whitelist()
@handle_api_errors
def delete_address(name):
    _require_login()
    _get_owned_address(name)
    frappe.delete_doc("SM Address", name, ignore_permissions=True)
    return {"ok": True}


@frappe.whitelist()
@handle_api_errors
def set_default_address(name):
    _require_login()
    doc = _get_owned_address(name)
    doc.is_default = 1
    doc.save(ignore_permissions=True)
    return {"ok": True}
