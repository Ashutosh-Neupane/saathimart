import frappe
from frappe.model.document import Document
from frappe.utils import flt


class SaathiOrder(Document):
	def validate(self):
		self.validate_single_franchise()
		self.calculate_totals()

	def validate_single_franchise(self):
		for row in self.items:
			if row.franchise and row.franchise != self.franchise:
				frappe.throw(
					f"Row #{row.idx}: item belongs to franchise {row.franchise}, "
					f"but this order is for {self.franchise}. An order can only contain items "
					"from a single franchise."
				)

	def calculate_totals(self):
		subtotal = 0.0
		for row in self.items:
			row.amount = flt(row.qty) * flt(row.rate)
			subtotal += row.amount
		self.subtotal = subtotal
		self.grand_total = subtotal + flt(self.delivery_charges)
