# Accounting System Improvements for Saathimart

## Executive Summary

This document outlines critical improvements needed for the three-party clearing house accounting model used in Saathimart marketplace.

---

## Current Architecture

### Three-Party Clearing House Model

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    THREE-PARTY ACCOUNTING FLOW                              │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  CUSTOMER          PLATFORM (Saathimart)         VENDOR                     │
│  ────────          ─────────────────────         ──────                     │
│                                                                             │
│  Payment ────────► Cash/Bank (DR)                                            │
│                    Revenue (CR)                                              │
│                    VAT Output (CR)                                           │
│                    Clearing-Vendor (DR) ──────► Clearing-Platform (DR)       │
│                                                     Revenue (CR)             │
│                                                     VAT Output (CR)          │
│                                                                             │
│  Settlement ──────────────────────────────────► Bank (DR)                   │
│                    Bank (CR)                      Commission Exp (DR)       │
│                    Clearing-Vendor (DR)           Clearing-Platform (CR)     │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Key Principle
**Vendor NEVER receives cash from customer directly.** Platform collects payment, holds in Clearing Account, settles weekly/bi-weekly.

---

## Critical Issues Found

### Issue 1: Missing Cost Center in GL Entries

**Problem**: ERPNext requires `cost_center` for P&L accounts (Income/Expense). Current code doesn't set it consistently.

**Impact**: GL Entry creation fails on fresh ERPNext installations.

**Fix**: Add cost center resolution to all GL entry creation functions.

### Issue 2: No Settlement Reconciliation

**Problem**: When platform pays vendor, there's no matching of which orders are being settled.

**Impact**: Clearing account balance tracking is inaccurate.

**Fix**: Add `Vendor Payout Order` child table linking settlements to specific orders.

### Issue 3: Commission VAT Not Tracked Separately

**Problem**: Commission is subject to 13% service VAT (input VAT for vendor, output VAT for platform). Current code doesn't always track this.

**Impact**: VAT reconciliation at month-end is difficult.

### Issue 4: No Delivery VAT Separation

**Problem**: Delivery charges have separate VAT (logistics entity). Current code mixes with product VAT.

**Impact**: Tax filing complications.

---

## Recommended Chart of Accounts

### Platform Accounts (Saathimart)

```python
# Run: bench --site saathimart.local execute saathimart.api.setup_accounts.setup_platform_accounts

PLATFORM_ACCOUNTS = {
    # Asset Accounts
    "cash_bank": "Cash/Bank - SM",
    "clearing_vendor": "Clearing Account - Vendor - SM",  # Money owed to vendors
    "clearing_logistics": "Clearing Account - Logistics - SM",  # Money owed to delivery partners
    "accounts_receivable": "Accounts Receivable - SM",
    "vat_input": "Input VAT - SM",  # VAT claimable on commission
    
    # Liability Accounts
    "vat_output": "Output VAT - SM",  # VAT collected on sales
    "accounts_payable": "Accounts Payable - SM",
    
    # Income Accounts
    "revenue": "Product Revenue - SM",
    "commission_income": "Marketplace Commission - SM",
    "delivery_income": "Delivery Service Income - SM",
    
    # Expense Accounts
    "platform_coupon_exp": "Platform Coupon Expense - SM",
    "loyalty_expense": "Loyalty Points Expense - SM",
}
```

### Vendor Accounts (Saathimart as Vendor Portal)

```python
# Run: bench --site saathimart.local execute saathimart.api.setup_accounts.setup_vendor_accounts

VENDOR_ACCOUNTS = {
    # Asset Accounts
    "cash_bank": "Cash/Bank",
    "clearing_platform": "SaathiMart Clearing",  # Money owed by platform
    "accounts_receivable": "Accounts Receivable",
    "vat_input": "Input VAT",
    
    # Liability Accounts
    "vat_output": "Output VAT",
    "accounts_payable": "Accounts Payable",
    
    # Income Accounts
    "revenue": "Sales",
    "platform_coupon_income": "Platform Coupon Reimbursement",
    "loyalty_income": "Loyalty Reimbursement",
    
    # Expense Accounts
    "commission_expense": "Marketplace Commission",
}
```

---

## Complete Accounting Flow

### 1. Customer Places Order (COD)

```
No GL entries until payment confirmed.
Order created with payment_status = "Unpaid".
```

### 2. Customer Pays (eSewa/Khalti/Card)

**Platform Side** (`record_order_payment_gl`):
```
DR: Cash/Bank - SM              NPR 1,130  (money received)
CR: Product Revenue - SM       NPR 1,000  (product sales)
CR: Output VAT - SM            NPR 130   (13% VAT)
DR: Clearing Account - Vendor  NPR 1,000  (owed to vendor)
```

**Vendor Side** (on `payment.received` event):
```
DR: SaathiMart Clearing        NPR 1,000  (platform owes)
CR: Sales                      NPR 885   (after commission)
CR: Output VAT                 NPR 115   (13% VAT)
```

### 3. Platform Coupon Applied

If coupon was platform-absorbed (e.g., NPR 100 off):

**Platform Side**:
```
DR: Platform Coupon Expense    NPR 100
CR: Clearing Account - Vendor  NPR 100   (reimburse vendor)
```

**Vendor Side**:
```
DR: SaathiMart Clearing        NPR 100   (platform owes)
CR: Platform Coupon Reimbursement  NPR 100   (income)
```

### 4. Settlement (Weekly/Bi-Weekly)

**Platform creates Vendor Payout**:
```
Total sales: NPR 10,000
Commission (5%): NPR 500
Platform coupons absorbed: NPR 200
Loyalty reimbursed: NPR 150
Net payout: NPR 9,850
```

**Platform Journal Entry**:
```
DR: Clearing Account - Vendor  NPR 10,000  (clear liability)
CR: Cash/Bank - SM             NPR 9,850   (paid to vendor)
CR: Marketplace Commission     NPR 500     (platform income)
DR: Platform Coupon Expense    NPR 200     (already recorded)
DR: Loyalty Expense            NPR 150     (already recorded)
```

**Vendor Journal Entry**:
```
DR: Cash/Bank                  NPR 9,850   (money received)
DR: Commission Expense         NPR 500     (platform fee)
CR: SaathiMart Clearing        NPR 10,350  (clears receivable + reimbursements)
```

---

## Delivery Charge Accounting

### Option A: Platform Handles Delivery (Current)

```
Customer pays delivery to platform.
Platform pays delivery partner separately.

Platform GL:
  DR: Cash/Bank                 NPR 100
  CR: Delivery Income           NPR 88.50
  CR: Output VAT                NPR 11.50
  
When paying delivery partner:
  DR: Clearing Account - Logistics  NPR 100
  CR: Cash/Bank                    NPR 100
```

### Option B: Vendor Handles Delivery (Recommended for marketplace)

```
Customer pays delivery to platform.
Platform passes to vendor.

Vendor charges delivery VAT separately.
Delivery income goes to vendor, not platform.
```

---

## Recommended Improvements

### 1. Add Settlement Matching

Create child table in Vendor Payout:

```python
# saathimart/saathimart/doctype/vendor_payout_order/vendor_payout_order.json
{
  "fields": [
    {"fieldname": "order", "fieldtype": "Link", "options": "Order"},
    {"fieldname": "vendor_fulfillment", "fieldtype": "Link", "options": "Vendor Fulfillment"},
    {"fieldname": "subtotal", "fieldtype": "Currency"},
    {"fieldname": "commission", "fieldtype": "Currency"},
    {"fieldname": "coupon_reimbursement", "fieldtype": "Currency"},
    {"fieldname": "loyalty_reimbursement", "fieldtype": "Currency"},
    {"fieldname": "net_amount", "fieldtype": "Currency"},
  ]
}
```

### 2. Add Reconciliation Report

```python
@frappe.whitelist()
def get_clearing_reconciliation(vendor, as_of_date=None):
    """
    Show all orders in clearing account and their settlement status.
    
    Returns:
      - Total in clearing (unpaid orders)
      - Total settled (linked to Vendor Payout)
      - Variance (should be zero)
    """
    pass
```

### 3. Add Cost Center Resolution

```python
def _get_default_cost_center(company):
    """Get or create a default cost center for marketplace operations."""
    cc = frappe.db.get_value(
        "Cost Center",
        {"company": company, "cost_center_name": "Marketplace", "is_group": 0},
        "name"
    )
    if cc:
        return cc
    
    # Create if not exists
    root = frappe.db.get_value(
        "Cost Center",
        {"company": company, "is_group": 1},
        "name"
    )
    if not root:
        return None
    
    doc = frappe.new_doc("Cost Center")
    doc.cost_center_name = "Marketplace"
    doc.company = company
    doc.parent_cost_center = root
    doc.is_group = 0
    doc.insert(ignore_permissions=True)
    return doc.name
```

### 4. Add Trial Balance Check

```python
def verify_clearing_balance(vendor):
    """
    Verify that the sum of GL entries in Clearing Account matches
    the sum of unpaid Vendor Fulfillments.
    """
    gl_balance = frappe.db.sql("""
        SELECT SUM(debit) - SUM(credit) as balance
        FROM `tabGL Entry`
        WHERE account = %s
    """, (clearing_account,))[0][0]
    
    fulfillment_balance = frappe.db.sql("""
        SELECT SUM(vf.subtotal)
        FROM `tabVendor Fulfillment` vf
        JOIN `tabOrder` o ON o.name = vf.parent
        WHERE vf.vendor = %s
          AND vf.status != 'Cancelled'
          AND (vf.vendor_payout IS NULL OR vf.vendor_payout = '')
          AND o.payment_status = 'Paid'
    """, (vendor,))[0][0]
    
    variance = flt(gl_balance) - flt(fulfillment_balance)
    
    return {
        "gl_balance": flt(gl_balance),
        "fulfillment_balance": flt(fulfillment_balance),
        "variance": flt(variance),
        "ok": abs(variance) < 0.01
    }
```

---

## Migration Path

### For Existing Installations

1. **Run account setup**:
   ```bash
   bench --site saathimart.local execute saathimart.api.setup_accounts.setup_platform_accounts
   ```

2. **Create default cost center**:
   ```bash
   bench --site saathimart.local execute saathimart.api.setup_accounts.create_marketplace_cost_center
   ```

3. **Reconcile existing orders**:
   ```bash
   bench --site saathimart.local execute saathimart.api.accounting.reconcile_clearing_accounts
   ```

### For New Installations

1. Install ERPNext
2. Create Company
3. Run setup_accounts
4. Configure Vendor Config
5. Start accepting orders

---

## Testing Checklist

### Payment Flow Testing

- [ ] Customer pays COD order
- [ ] Customer pays eSewa order
- [ ] Customer pays Khalti order
- [ ] Customer pays card order

### Coupon Testing

- [ ] Vendor-absorbed coupon (reduces vendor revenue)
- [ ] Platform-absorbed coupon (platform reimburses)
- [ ] Free delivery coupon
- [ ] Percentage coupon with max limit
- [ ] Fixed amount coupon
- [ ] Per-customer limit enforcement

### Settlement Testing

- [ ] Create vendor payout
- [ ] Verify clearing balance cleared
- [ ] Verify vendor receives correct amount
- [ ] Verify commission calculated correctly
- [ ] Verify coupon reimbursement included
- [ ] Verify loyalty reimbursement included

### Reconciliation Testing

- [ ] Run trial balance
- [ ] Verify clearing account matches unpaid fulfillments
- [ ] Verify no orphaned GL entries

---

## Questions for Production Deployment

1. **VAT Registration**: Is platform VAT registered separately from vendors?
2. **Delivery Partner**: Who handles delivery - platform or vendor?
3. **Commission Rate**: Fixed or per-vendor?
4. **Settlement Frequency**: Weekly, bi-weekly, or monthly?
5. **Minimum Payout**: Threshold for automatic settlement?

---

## Next Steps

1. Review this document with your accountant
2. Run `setup_accounts` on your development site
3. Test complete flow with sample orders
4. Deploy to staging environment
5. Train finance team on reconciliation reports
