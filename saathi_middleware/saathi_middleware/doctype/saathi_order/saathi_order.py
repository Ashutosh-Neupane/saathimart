import frappe
from frappe.model.document import Document
from frappe.utils import flt


class SaathiOrder(Document):
	def validate(self):
		self.validate_single_franchise()
		self.calculate_subtotal()
		self.apply_coupon()
		self.apply_loyalty_redemption()
		self.calculate_grand_total()

	def after_insert(self):
		self._earn_loyalty_points()
		self._redeem_loyalty_points()

	def _earn_loyalty_points(self):
		if not self.customer_email or not self.grand_total:
			return
		from saathi_middleware.api.loyalty import earn_points
		points = earn_points(self.customer_email, self.name, flt(self.grand_total))
		if points:
			self.db_set("loyalty_points_earned", points, update_modified=False)

	def _redeem_loyalty_points(self):
		if not self.loyalty_points_redeemed or not self.customer_email:
			return
		from saathi_middleware.api.loyalty import redeem_points
		redeem_points(
			self.customer_email, self.name,
			flt(self.loyalty_points_redeemed), flt(self.loyalty_discount or 0),
		)

	def validate_single_franchise(self):
		for row in self.items:
			if row.franchise and row.franchise != self.franchise:
				frappe.throw(
					f"Row #{row.idx}: item belongs to franchise {row.franchise}, "
					f"but this order is for {self.franchise}. An order can only contain items "
					"from a single franchise."
				)

	def apply_coupon(self):
		if not self.coupon_code:
			return
		from saathi_middleware.saathi_middleware.doctype.saathi_coupon.saathi_coupon import validate_coupon
		result = validate_coupon(
			self.coupon_code,
			self.franchise,
			self.customer_mobile,
			flt(self.subtotal or 0),
		)
		self.coupon_discount = flt(result.get("discount", 0))

	def apply_loyalty_redemption(self):
		if not self.loyalty_points_redeemed:
			return
		from saathi_middleware.api.loyalty import calculate_redemption_discount
		result = calculate_redemption_discount(
			self.customer_email or "",
			flt(self.loyalty_points_redeemed),
			flt(self.subtotal or 0),
		)
		if result.get("ok"):
			self.loyalty_discount = flt(result.get("discount", 0))
			self.loyalty_points_redeemed = flt(result.get("points_used", 0))
		else:
			# Reset rather than leave the customer's unvalidated request on the
			# order — otherwise after_insert would try to redeem points that
			# were never actually approved (loyalty disabled, insufficient
			# balance, below the program minimum, ...).
			self.loyalty_discount = 0
			self.loyalty_points_redeemed = 0

	def calculate_subtotal(self):
		subtotal = 0.0
		for row in self.items:
			row.amount = flt(row.qty) * flt(row.rate)
			subtotal += row.amount
		self.subtotal = subtotal

	def calculate_grand_total(self):
		self.grand_total = (
			flt(self.subtotal)
			+ flt(self.delivery_charges)
			- flt(self.coupon_discount or 0)
			- flt(self.loyalty_discount or 0)
		)
