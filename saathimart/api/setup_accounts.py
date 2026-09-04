"""
Setup Chart of Accounts for Three-Party Marketplace Clearing House

Creates all required accounts in ERPNext for:
  - SaathiMart Platform (Entity A)
  - SaathiMart Vendor (Entity B) - run on vendor sites
  - Logistics Partner (Entity C) - optional

Run with: bench --site <site_name> execute saathimart.api.setup_accounts.setup_platform_accounts
"""
from __future__ import annotations

import frappe
from frappe import _


def setup_platform_accounts():
    """Create Chart of Accounts for the SaathiMart Platform."""
    company = frappe.defaults.get_global_default("company")
    if not company:
        company = frappe.db.get_value("Company", {}, "name")
    if not company:
        frappe.throw(_("No Company found. Create a Company first."))

    accounts = [
        # ── Revenue Accounts ──
        {"account_name": "Product Revenue", "account_type": "Income", "root_type": "Income"},
        {"account_name": "Marketplace Commission", "account_type": "Income", "root_type": "Income"},
        {"account_name": "Delivery Service Income", "account_type": "Income", "root_type": "Income"},
        
        # ── Expense Accounts ──
        {"account_name": "Platform Coupon Expense", "account_type": "Expense", "root_type": "Expense"},
        {"account_name": "Loyalty Points Expense", "account_type": "Expense", "root_type": "Expense"},
        
        # ── Asset Accounts (Clearing) ──
        {"account_name": "Clearing Account - Vendor", "account_type": "Current Asset", "root_type": "Asset"},
        {"account_name": "Clearing Account - Logistics", "account_type": "Current Asset", "root_type": "Asset"},
        
        # ── Tax Accounts ──
        {"account_name": "Output VAT", "account_type": "Tax", "root_type": "Liability"},
        {"account_name": "Input VAT", "account_type": "Tax", "root_type": "Asset"},
    ]

    created = 0
    for acc in accounts:
        account_name = f"{acc['account_name']} - {company}"
        if not frappe.db.exists("Account", account_name):
            doc = frappe.new_doc("Account")
            doc.account_name = acc["account_name"]
            doc.company = company
            doc.account_type = acc["account_type"]
            doc.root_type = acc["root_type"]
            doc.insert(ignore_permissions=True)
            created += 1
            print(f"  Created: {account_name}")
        else:
            print(f"  Exists:  {account_name}")

    print(f"\nPlatform accounts setup complete. Created {created} new accounts.")
    return created


def setup_vendor_accounts():
    """Create Chart of Accounts for SaathiMart-Vendor sites."""
    company = frappe.defaults.get_global_default("company")
    if not company:
        company = frappe.db.get_value("Company", {}, "name")
    if not company:
        frappe.throw(_("No Company found. Create a Company first."))

    accounts = [
        # ── Revenue Accounts ──
        {"account_name": "Sales", "account_type": "Income", "root_type": "Income"},
        
        # ── Expense Accounts ──
        {"account_name": "Marketplace Commission", "account_type": "Expense", "root_type": "Expense"},
        
        # ── Income Accounts (Reimbursements) ──
        {"account_name": "Platform Coupon Reimbursement", "account_type": "Income", "root_type": "Income"},
        {"account_name": "Loyalty Reimbursement", "account_type": "Income", "root_type": "Income"},
        
        # ── Asset Accounts (Clearing) ──
        {"account_name": "SaathiMart Clearing", "account_type": "Current Asset", "root_type": "Asset"},
        
        # ── Tax Accounts ──
        {"account_name": "Output VAT", "account_type": "Tax", "root_type": "Liability"},
        {"account_name": "Input VAT", "account_type": "Tax", "root_type": "Asset"},
    ]

    created = 0
    for acc in accounts:
        account_name = f"{acc['account_name']} - {company}"
        if not frappe.db.exists("Account", account_name):
            doc = frappe.new_doc("Account")
            doc.account_name = acc["account_name"]
            doc.company = company
            doc.account_type = acc["account_type"]
            doc.root_type = acc["root_type"]
            doc.insert(ignore_permissions=True)
            created += 1
            print(f"  Created: {account_name}")
        else:
            print(f"  Exists:  {account_name}")

    print(f"\nVendor accounts setup complete. Created {created} new accounts.")
    return created


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "vendor":
        setup_vendor_accounts()
    else:
        setup_platform_accounts()
