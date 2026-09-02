"""Audit logging API for Saathimart.

Tracks who changed what, when, and how.
"""
import frappe
import json
from frappe.utils import now_datetime


def log_audit_entry(doc, method=None):
    """Log an audit entry for document changes.

    Called automatically via doc_events hook on SM Audit Log.
    """
    try:
        # This is called automatically when an SM Audit Log is created
        # The actual logging happens in the document's methods
        pass
    except Exception:
        frappe.log_error(f"Failed to process audit log entry", "audit_log")


@frappe.whitelist()
def create_audit_log(
    ref_doctype,
    docname,
    action,
    user=None,
    changes=None,
    old_value=None,
    new_value=None,
):
    """Create an audit log entry.

    Args:
        ref_doctype: The DocType being audited
        docname: The document name
        action: Create, Update, Delete, Approve, Reject
        user: User who made the change (optional, defaults to current user)
        changes: JSON of changed fields
        old_value: JSON of old values
        new_value: JSON of new values

    Returns:
        dict with audit_log name
    """
    if not user:
        user = frappe.session.user

    log = frappe.new_doc("SM Audit Log")
    log.ref_doctype = ref_doctype
    log.docname = docname
    log.action = action
    log.user = user
    log.changes = json.dumps(changes) if changes else None
    log.old_value = json.dumps(old_value) if old_value else None
    log.new_value = json.dumps(new_value) if new_value else None
    log.insert(ignore_permissions=True)
    frappe.db.commit()

    return {"audit_log": log.name, "ok": True}


def log_product_update(doc, method):
    """Log product updates."""
    if method == "after_insert":
        create_audit_log(
            ref_doctype="Product",
            docname=doc.name,
            action="Create",
            new_value=json.dumps({
                "product_name": doc.product_name,
                "status": doc.status,
                "price": doc.price,
            }),
        )
    elif method == "on_update":
        # Get old values
        old_doc = frappe.get_doc("Product", doc.name)
        changes = {}
        old_values = {}
        new_values = {}
        
        # Compare common fields
        for field in ["product_name", "status", "price", "stock_qty", "avg_rating"]:
            if hasattr(old_doc, field) and hasattr(doc, field):
                old_val = getattr(old_doc, field)
                new_val = getattr(doc, field)
                if old_val != new_val:
                    changes[field] = True
                    old_values[field] = old_val
                    new_values[field] = new_val
        
        if changes:
            create_audit_log(
                ref_doctype="Product",
                docname=doc.name,
                action="Update",
                changes=changes,
                old_value=old_values,
                new_value=new_values,
            )


def log_order_update(doc, method):
    """Log order status updates."""
    if method == "on_update":
        old_doc = frappe.get_doc("Order", doc.name)
        
        if old_doc.status != doc.status:
            create_audit_log(
                ref_doctype="Order",
                docname=doc.name,
                action="Update" if doc.status != "Pending" else "Create",
                changes={"status": True},
                old_value={"status": old_doc.status},
                new_value={"status": doc.status},
            )