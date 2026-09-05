import frappe
from frappe.model.document import Document
from frappe.utils import flt
from saathimart.api.totals import calculate_taxes_and_totals


class Order(Document):
    """
    Orders are never submitted/cancelled through Frappe's docstatus
    lifecycle — status moves through the `status` Select field via
    api.orders.update_order_status(). Stock is reserved per vendor at
    checkout (api.orders.checkout → api.stock.atomic_reserve) and
    released/confirmed on the Cancelled/Delivered transitions
    (see update_order_status()) — not here.
    """

    def validate(self):
        # Validation: Order must have at least one item
        if not self.items or len(self.items) == 0:
            frappe.throw(_("Order must contain at least one item"), title="Empty Order")

        # Validation: All items must have positive quantity and rate
        for item in self.items:
            if flt(item.qty) <= 0:
                frappe.throw(_("Item '{0}' must have a positive quantity").format(item.product), title="Invalid Quantity")
            if flt(item.rate) < 0:
                frappe.throw(_("Item '{0}' must have a non-negative rate").format(item.product), title="Invalid Rate")

        # Validation: Grand total must be positive
        if flt(self.grand_total) <= 0:
            frappe.throw(_("Order grand total must be positive"), title="Invalid Total")

        # Run the full ERPNext-style totals engine on every save
        calculate_taxes_and_totals(self)

    def _redeem_loyalty_points(self):
        if not (self.loyalty_points_redeemed and self.loyalty_discount and self.customer_email):
            return
        from saathimart.api.loyalty import redeem_points
        redeem_points(
            self.customer_email,
            self.name,
            self.loyalty_points_redeemed,
            self.loyalty_discount,
        )

    def after_insert(self):
        # Redemption is a one-time debit against the order being placed,
        # regardless of payment method.
        self._redeem_loyalty_points()

        # Membership savings are banked at placement, not delivery: the
        # discount was already taken off what the customer pays, so the ledger
        # must agree with the invoice even if the order is later cancelled.
        self._record_membership_savings()

        # Earn points when order is placed (COD earns on delivery, online on payment)
        # For now earn on insert — adjust to on_payment_received if needed
        if self.payment_method == "COD":
            return  # earn on delivery confirmation instead
        self._earn_loyalty_points()

    def on_update(self):
        if self.status == "Delivered" and self.payment_method == "COD":
            self._earn_loyalty_points()

    def _record_membership_savings(self):
        """Best-effort: a ledger failure must never block a placed order."""
        if not self.get("membership"):
            return
        try:
            from saathimart.api.membership import record_savings
            record_savings(self)
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"Membership savings ledger failed for {self.name}")

    def _earn_loyalty_points(self):
        if not self.customer_email:
            return
        from saathimart.api.loyalty import earn_points
        earned = earn_points(self.customer_email, self.name, self.grand_total)
        if earned:
            self.db_set("loyalty_points_earned", earned, update_modified=False)
