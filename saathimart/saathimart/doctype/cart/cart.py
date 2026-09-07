import frappe
from frappe.model.document import Document
from frappe.utils import add_days, now_datetime, flt


class Cart(Document):
    def before_insert(self):
        if not self.expires_at:
            self.expires_at = add_days(now_datetime(), 7)

    def validate(self):
        self.subtotal = sum(
            (item.qty or 0) * (item.rate or 0) for item in self.items
        )
        for item in self.items:
            item.amount = (item.qty or 0) * (item.rate or 0)

        # NOTE: an empty cart is a legitimate state — _get_or_create_cart
        # creates the shell before the first item and clear_cart() empties
        # it on demand. Enforcing non-emptiness here made both impossible
        # (chicken-and-egg: set_customer_location/create before add_to_cart,
        # and clear). Empty-cart enforcement lives at checkout() instead.

        # Validation: All items must have positive quantity and rate
        for item in self.items:
            if flt(item.qty) <= 0:
                frappe.throw(_("Item '{0}' must have a positive quantity").format(item.product), title="Invalid Quantity")
            if flt(item.rate) < 0:
                frappe.throw(_("Item '{0}' must have a non-negative rate").format(item.product), title="Invalid Rate")

    def before_save(self):
        # Update subtotal before saving
        self.subtotal = sum(flt(item.qty) * flt(item.rate) for item in self.items or [])
