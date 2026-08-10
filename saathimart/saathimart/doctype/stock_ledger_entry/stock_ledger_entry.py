import frappe
from frappe.utils import flt, nowdate, nowtime
from frappe.model.document import Document


class StockLedgerEntry(Document):
    pass


def make_entry(product, qty_change, voucher_type, voucher_no,
               source_site="", vendor=None, remarks=""):
    """
    Create one SLE row and update Product.stock_qty atomically.
    qty_change: negative = stock out, positive = stock in.
    Returns the new balance_qty.
    """
    track_inventory, current_qty = frappe.db.get_value(
        "Product", product, ["track_inventory", "stock_qty"], for_update=True
    )
    if not track_inventory:
        return flt(current_qty)

    new_balance = flt(current_qty) + flt(qty_change)

    sle = frappe.new_doc("Stock Ledger Entry")
    sle.posting_date = nowdate()
    sle.posting_time = nowtime()
    sle.product      = product
    sle.voucher_type = voucher_type
    sle.voucher_no   = voucher_no
    sle.qty_change   = flt(qty_change)
    sle.balance_qty  = new_balance
    sle.source_site  = source_site or ""
    sle.vendor       = vendor or ""
    sle.remarks      = remarks or ""
    sle.insert(ignore_permissions=True)

    frappe.db.set_value("Product", product, "stock_qty", new_balance, update_modified=False)
    return new_balance
