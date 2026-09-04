"""
Setup Chart of Accounts for Three-Party Marketplace Clearing House

Creates all required accounts in ERPNext for:
  - SaathiMart Platform (Entity A)
  - SaathiMart Vendor (Entity B) - run on vendor sites
  - Logistics Partner (Entity C) - optional

Run with: 
  bench --site <site_name> execute saathimart.api.setup_accounts.setup_platform_accounts
  bench --site <site_name> execute saathimart.api.setup_accounts.setup_vendor_accounts
  bench --site <site_name> execute saathimart.api.setup_accounts.create_marketplace_cost_center
"""
from __future__ import annotations

import frappe
from frappe import _


def _get_company():
    """Get the default company."""
    company = frappe.defaults.get_global_default("company")
    if not company:
        company = frappe.db.get_value("Company", {}, "name")
    if not company:
        frappe.throw(_("No Company found. Create a Company first."))
    return company


def _get_root_account(company, root_type):
    """Get or create the root account for a given root type."""
    # Try to find existing root
    root = frappe.db.get_value(
        "Account",
        {"company": company, "root_type": root_type, "is_group": 1, "parent_account": None},
        "name"
    )
    if root:
        return root
    
    # Create root if not exists
    try:
        doc = frappe.new_doc("Account")
        doc.account_name = root_type
        doc.company = company
        doc.root_type = root_type
        doc.is_group = 1
        doc.insert(ignore_permissions=True, ignore_mandatory=True)
        return doc.name
    except Exception:
        return None


def _create_account(account_name, company, account_type, root_type, parent_account=None):
    """Create a single account with proper parent."""
    # Check if already exists (ERPNext appends company abbreviation)
    existing = frappe.db.get_value(
        "Account",
        {"company": company, "account_name": account_name},
        "name"
    )
    if existing:
        print(f"  Exists:  {existing}")
        return existing
    
    # Get parent if not specified
    if not parent_account:
        parent_account = _get_root_account(company, root_type)
    
    if not parent_account:
        print(f"  Skip:    {account_name} (no parent account found)")
        return None
    
    try:
        doc = frappe.new_doc("Account")
        doc.account_name = account_name
        doc.company = company
        doc.account_type = account_type
        doc.root_type = root_type
        doc.parent_account = parent_account
        doc.is_group = 0
        doc.insert(ignore_permissions=True)
        print(f"  Created: {doc.name}")
        return doc.name
    except Exception as e:
        print(f"  Error:   {account_name} - {str(e)}")
        return None


def create_marketplace_cost_center():
    """Create a dedicated cost center for marketplace operations."""
    company = _get_company()
    
    # Check if already exists
    existing = frappe.db.get_value(
        "Cost Center",
        {"company": company, "cost_center_name": "Marketplace"},
        "name"
    )
    if existing:
        print(f"Cost center already exists: {existing}")
        return existing
    
    # Get root cost center
    root = frappe.db.get_value(
        "Cost Center",
        {"company": company, "is_group": 1},
        "name"
    )
    if not root:
        print("No root cost center found. Please run ERPNext setup first.")
        return None
    
    try:
        doc = frappe.new_doc("Cost Center")
        doc.cost_center_name = "Marketplace"
        doc.company = company
        doc.parent_cost_center = root
        doc.is_group = 0
        doc.insert(ignore_permissions=True)
        print(f"Created cost center: {doc.name}")
        return doc.name
    except Exception as e:
        print(f"Error creating cost center: {e}")
        return None


def setup_platform_accounts():
    """Create Chart of Accounts for the SaathiMart Platform.
    
    Creates accounts for:
    - Product revenue (marketplace sales)
    - Commission income (vendor commission)
    - Delivery income (delivery charges)
    - Platform coupon expense (coupons absorbed by platform)
    - Loyalty expense (loyalty points reimbursement)
    - Clearing accounts (vendor, logistics)
    - VAT accounts (input, output)
    """
    company = _get_company()
    print(f"\nSetting up platform accounts for: {company}\n")
    
    created = 0
    
    # ── Income Accounts ──
    print("Income Accounts:")
    income_root = _get_root_account(company, "Income")
    
    if _create_account("Product Revenue - SM", company, "Income Account", "Income", income_root):
        created += 1
    if _create_account("Marketplace Commission - SM", company, "Income Account", "Income", income_root):
        created += 1
    if _create_account("Delivery Service Income - SM", company, "Income Account", "Income", income_root):
        created += 1
    
    # ── Expense Accounts ──
    print("\nExpense Accounts:")
    expense_root = _get_root_account(company, "Expense")
    
    if _create_account("Platform Coupon Expense - SM", company, "Expense Account", "Expense", expense_root):
        created += 1
    if _create_account("Loyalty Points Expense - SM", company, "Expense Account", "Expense", expense_root):
        created += 1
    
    # ── Asset Accounts (Clearing) ──
    print("\nAsset Accounts:")
    asset_root = _get_root_account(company, "Asset")
    
    if _create_account("Clearing Account - Vendor - SM", company, "Current Asset", "Asset", asset_root):
        created += 1
    if _create_account("Clearing Account - Logistics - SM", company, "Current Asset", "Asset", asset_root):
        created += 1
    if _create_account("Input VAT - SM", company, "Tax", "Asset", asset_root):
        created += 1
    
    # ── Liability Accounts ──
    print("\nLiability Accounts:")
    liability_root = _get_root_account(company, "Liability")
    
    if _create_account("Output VAT - SM", company, "Tax", "Liability", liability_root):
        created += 1
    
    # ── Create Cost Center ──
    print("\nCost Center:")
    if create_marketplace_cost_center():
        created += 1
    
    print(f"\n✅ Platform accounts setup complete. Created {created} new items.")
    return created


def setup_vendor_accounts():
    """Create Chart of Accounts for SaathiMart-Vendor sites.
    
    Creates accounts for:
    - Sales revenue
    - Marketplace commission expense
    - Platform coupon reimbursement (income)
    - Loyalty reimbursement (income)
    - SaathiMart Clearing (receivable from platform)
    - VAT accounts (input, output)
    """
    company = _get_company()
    print(f"\nSetting up vendor accounts for: {company}\n")
    
    created = 0
    
    # ── Income Accounts ──
    print("Income Accounts:")
    income_root = _get_root_account(company, "Income")
    
    if _create_account("Sales - Vendor", company, "Income Account", "Income", income_root):
        created += 1
    if _create_account("Platform Coupon Reimbursement - Vendor", company, "Income Account", "Income", income_root):
        created += 1
    if _create_account("Loyalty Reimbursement - Vendor", company, "Income Account", "Income", income_root):
        created += 1
    
    # ── Expense Accounts ──
    print("\nExpense Accounts:")
    expense_root = _get_root_account(company, "Expense")
    
    if _create_account("Marketplace Commission Expense - Vendor", company, "Expense Account", "Expense", expense_root):
        created += 1
    
    # ── Asset Accounts (Clearing) ──
    print("\nAsset Accounts:")
    asset_root = _get_root_account(company, "Asset")
    
    if _create_account("SaathiMart Clearing - Vendor", company, "Current Asset", "Asset", asset_root):
        created += 1
    if _create_account("Input VAT - Vendor", company, "Tax", "Asset", asset_root):
        created += 1
    
    # ── Liability Accounts ──
    print("\nLiability Accounts:")
    liability_root = _get_root_account(company, "Liability")
    
    if _create_account("Output VAT - Vendor", company, "Tax", "Liability", liability_root):
        created += 1
    
    # ── Create Cost Center ──
    print("\nCost Center:")
    if create_marketplace_cost_center():
        created += 1
    
    print(f"\n✅ Vendor accounts setup complete. Created {created} new items.")
    return created


def verify_accounts_setup(entity="platform"):
    """Verify that all required accounts exist."""
    company = _get_company()
    
    if entity == "platform":
        required_accounts = [
            "Product Revenue - SM",
            "Marketplace Commission - SM",
            "Delivery Service Income - SM",
            "Platform Coupon Expense - SM",
            "Loyalty Points Expense - SM",
            "Clearing Account - Vendor - SM",
            "Clearing Account - Logistics - SM",
            "Output VAT - SM",
            "Input VAT - SM",
        ]
    else:
        required_accounts = [
            "Sales - Vendor",
            "Marketplace Commission Expense - Vendor",
            "Platform Coupon Reimbursement - Vendor",
            "Loyalty Reimbursement - Vendor",
            "SaathiMart Clearing - Vendor",
            "Output VAT - Vendor",
            "Input VAT - Vendor",
        ]
    
    print(f"\nVerifying {entity} accounts for: {company}\n")
    
    missing = []
    for account_name in required_accounts:
        exists = frappe.db.exists("Account", {"company": company, "account_name": account_name})
        if exists:
            print(f"  ✅ {account_name}")
        else:
            print(f"  ❌ {account_name} (MISSING)")
            missing.append(account_name)
    
    if missing:
        print(f"\n⚠️  Missing {len(missing)} accounts. Run setup_{entity}_accounts() to create them.")
        return False
    else:
        print(f"\n✅ All {entity} accounts verified.")
        return True


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        if sys.argv[1] == "vendor":
            setup_vendor_accounts()
        elif sys.argv[1] == "verify":
            entity = sys.argv[2] if len(sys.argv) > 2 else "platform"
            verify_accounts_setup(entity)
        elif sys.argv[1] == "cost-center":
            create_marketplace_cost_center()
    else:
        setup_platform_accounts()
