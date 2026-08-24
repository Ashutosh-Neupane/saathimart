"""
Seeds the Payment Mode registry matching what the storefront checkout sends:
"eSewa", "Bank Transfer", "COD". Idempotent — updates rather than duplicates
if a site admin already has these. Ported from saathi_middleware's
seed_payment_modes patch, minus the ERPNext mode-of-payment mapping (the hub
does not push to ERPNext).

Bank Transfer is deliberately is_online=0 (offline/manual reconciliation,
same as COD) — initiate_payment() routes ANY online mode through eSewa's
flow, so there is no way to mark a second mode online without it actually
being eSewa under the hood. Khalti stays out entirely (dropped as untested).
"""
import frappe


def execute():
    modes = [
        {"mode_name": "eSewa", "slug": "esewa", "is_enabled": 1, "is_online": 1,
         "gateway": "eSewa", "display_order": 1,
         "description": "Pay from your eSewa wallet."},
        {"mode_name": "Bank Transfer", "slug": "card", "is_enabled": 1, "is_online": 0,
         "display_order": 2,
         "description": "Use Card to complete payment."},
        {"mode_name": "COD", "slug": "cash-on-delivery", "is_enabled": 1, "is_online": 0,
         "display_order": 3,
         "description": "Pay our rider while delivering your product."},
    ]

    for m in modes:
        if frappe.db.exists("Payment Mode", m["mode_name"]):
            doc = frappe.get_doc("Payment Mode", m["mode_name"])
            for key, val in m.items():
                setattr(doc, key, val)
            doc.save(ignore_permissions=True)
        else:
            frappe.get_doc({"doctype": "Payment Mode", **m}).insert(ignore_permissions=True)

    # Legacy orders may carry raw strings that predate the registry.
    frappe.db.sql("""
        UPDATE `tabOrder`
        SET payment_method = 'eSewa'
        WHERE LOWER(payment_method) = 'esewa' AND payment_method != 'eSewa'
    """)

    frappe.db.commit()
