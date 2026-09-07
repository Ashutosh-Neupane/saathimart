"""Custom fields SaathiMart adds to core doctypes.

Wired into after_migrate so `bench migrate` (and install) always leaves the
schema complete. create_custom_fields is idempotent — existing fields are
updated in place, so it is safe on every migrate.
"""
import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

CUSTOM_FIELDS = {
    "User": [
        # Birthday rewards cron (loyalty.check_birthday_rewards) needs the
        # customer's date of birth on the User record.
        dict(fieldname="birthday", label="Birthday", fieldtype="Date",
             insert_after="birth_date"),
    ],
}


def ensure_custom_fields():
    try:
        create_custom_fields(CUSTOM_FIELDS)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "saathimart custom field setup")
