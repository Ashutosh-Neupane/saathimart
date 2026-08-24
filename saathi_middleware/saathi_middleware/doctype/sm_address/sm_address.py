import frappe
from frappe.model.document import Document


class SMAddress(Document):
	def before_save(self):
		if self.is_default:
			frappe.db.set_value(
				"SM Address",
				{"user": self.user, "name": ["!=", self.name or ""]},
				"is_default",
				0,
			)
