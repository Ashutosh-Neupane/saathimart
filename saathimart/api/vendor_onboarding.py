"""
Vendor onboarding — self-service registration, document upload,
admin approval workflow, and auto-provisioning.
"""
import frappe
from frappe import _
import secrets


@frappe.whitelelist(allow_guest=True)
def register_vendor(vendor_name, contact_email, contact_phone,
                    business_type="retail", address=""):
    """
    Vendor self-registration. Creates a Pending vendor record.
    Admin must approve before the vendor becomes active.
    """
    if not vendor_name or not contact_email:
        frappe.throw(_("Vendor name and contact email are required"))

    if frappe.db.exists("Vendor", {"vendor_name": vendor_name}):
        frappe.throw(_("A vendor with this name already exists"))

    # Generate slug
    slug = frappe.scrub(vendor_name).replace("_", "-")

    # Generate API credentials
    api_key = secrets.token_urlsafe(16)
    api_secret = secrets.token_urlsafe(32)
    webhook_secret = secrets.token_urlsafe(32)

    vendor = frappe.new_doc("Vendor")
    vendor.vendor_name = vendor_name
    vendor.slug = slug
    vendor.status = "Pending"
    vendor.contact_email = contact_email
    vendor.contact_phone = contact_phone
    vendor.address = address
    vendor.api_key = api_key
    vendor.flags.ignore_links = True
    vendor.insert(ignore_permissions=True)
    frappe.db.commit()

    # Store secrets securely
    from frappe.utils.password import set_encrypted_password
    set_encrypted_password("Vendor", vendor.name, api_secret, "api_secret")
    set_encrypted_password("Vendor", vendor.name, webhook_secret, "webhook_secret")
    frappe.db.commit()

    # Notify admin
    try:
        frappe.sendmail(
            recipients=frappe.db.get_single_value("Settings", "admin_email") or "admin@example.com",
            subject="New Vendor Registration: {0}".format(vendor_name),
            message="<p>A new vendor <strong>{0}</strong> has registered and needs approval.</p>"
                    "<p>Contact: {1} ({2})</p>"
                    "<p>Business type: {3}</p>".format(
                        vendor_name, contact_email, contact_phone, business_type
                    ),
        )
    except Exception:
        pass

    return {
        "ok": True,
        "vendor": vendor.name,
        "status": "Pending",
        "message": "Registration submitted. You will be notified once approved.",
    }


@frappe.whitelist()
def approve_vendor(vendor_name):
    """Admin approves a pending vendor."""
    if not frappe.db.exists("Vendor", vendor_name):
        frappe.throw(_("Vendor not found"))

    status = frappe.db.get_value("Vendor", vendor_name, "status")
    if status != "Pending":
        frappe.throw(_("Vendor is not in Pending status"))

    frappe.db.set_value("Vendor", vendor_name, {"status": "Active"})
    frappe.db.commit()

    # Notify vendor
    vendor = frappe.get_doc("Vendor", vendor_name)
    if vendor.contact_email:
        try:
            frappe.sendmail(
                recipients=[vendor.contact_email],
                subject="Vendor Account Approved!",
                message="<p>Your vendor account <strong>{0}</strong> has been approved!</p>"
                        "<p>You can now log in and start listing products.</p>".format(vendor_name),
            )
        except Exception:
            pass

    return {"ok": True, "vendor": vendor_name, "status": "Active"}


@frappe.whitelist()
def reject_vendor(vendor_name, reason=""):
    """Admin rejects a pending vendor."""
    frappe.db.set_value("Vendor", vendor_name, {"status": "Suspended"})
    frappe.db.commit()
    return {"ok": True, "vendor": vendor_name, "status": "Suspended"}


@frappe.whitelelist(allow_guest=True)
def get_onboarding_status(vendor_name):
    """Check onboarding status for a vendor."""
    if not frappe.db.exists("Vendor", vendor_name):
        return {"status": "not_found"}

    vendor = frappe.get_doc("Vendor", vendor_name)
    has_url = bool(vendor.frappe_site_url)
    has_location = bool(vendor.lat and vendor.lng)
    has_warehouse = bool(vendor.default_warehouse or (vendor.warehouses and len(vendor.warehouses) > 0))

    steps = {
        "registered": True,
        "approved": vendor.status == "Active",
        "site_configured": has_url,
        "location_set": has_location,
        "warehouse_configured": has_warehouse,
    }

    return {
        "vendor": vendor_name,
        "status": vendor.status,
        "steps": steps,
        "complete": all(steps.values()),
    }
