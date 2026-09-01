import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt


class SMProductReview(Document):
	def validate(self):
		if not (1 <= flt(self.rating) <= 5):
			frappe.throw(_("Rating must be between 1 and 5"))

	def on_update(self):
		_recompute_product_rating(self.product)

	def on_trash(self):
		_recompute_product_rating(self.product)


def _recompute_product_rating(product):
	stats = frappe.db.sql(
		"""
		SELECT COUNT(*) AS count, AVG(rating) AS avg
		FROM `tabSM Product Review`
		WHERE product = %s AND status = 'Approved'
		""",
		(product,),
		as_dict=True,
	)[0]
	frappe.db.set_value(
		"Product",
		product,
		{
			"avg_rating": round(flt(stats.avg or 0), 1),
			"review_count": stats.count or 0,
		},
		update_modified=False,
	)
