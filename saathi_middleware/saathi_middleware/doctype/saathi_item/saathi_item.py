import frappe
from frappe.model.document import Document


class SaathiItem(Document):
	def before_save(self):
		franchise = frappe.db.get_value(
			"Franchise", self.franchise, ["latitude", "longitude", "pincode", "city"], as_dict=True
		)
		if franchise:
			self.latitude = franchise.latitude
			self.longitude = franchise.longitude
			self.pincode = franchise.pincode
			self.city = franchise.city
