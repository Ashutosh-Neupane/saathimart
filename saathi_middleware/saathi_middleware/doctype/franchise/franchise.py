import frappe
from frappe.model.document import Document


class Franchise(Document):
	def before_insert(self):
		if not self.api_key:
			self.api_key = frappe.generate_hash(length=15)
		if not self.api_secret:
			self.api_secret = frappe.generate_hash(length=25)

	def after_insert(self):
		frappe.msgprint(
			f"API Key: {self.api_key}<br>"
			f"API Secret: {self.get_password('api_secret')}<br><br>"
			"Copy both into the franchise's Saathi Ecommerce Settings. The API Key stays visible on this "
			"record anytime; the Secret is also visible to a System Manager via this record later, but "
			"won't be echoed back in a popup again.",
			title="Franchise Credentials",
			indicator="green",
		)
