import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, today


class SaathiCoupon(Document):
	pass


def validate_coupon(coupon_code, franchise, customer_mobile, subtotal):
	"""
	Validate a Saathi Coupon against a franchise + customer + order
	subtotal, returning the discount amount. Raises frappe.ValidationError
	(via frappe.throw) on any failure — callers that want a soft {ok:
	false, message} response instead of a raised exception should catch
	frappe.ValidationError, matching api.coupon.validate's pattern.

	customer_mobile (not email) is the usage-limit key because checkout
	only requires a mobile number — customer_email is optional, so keying
	per-customer limits on it would let a guest bypass usage_limit_per_customer
	simply by leaving email blank.
	"""
	if not frappe.db.exists("Saathi Coupon", coupon_code):
		frappe.throw(_("Coupon {0} is not valid").format(coupon_code))
	coupon = frappe.get_doc("Saathi Coupon", coupon_code)
	if not coupon.is_active:
		frappe.throw(_("Coupon {0} is not valid").format(coupon_code))

	current_date = today()
	if coupon.valid_from and current_date < str(coupon.valid_from):
		frappe.throw(_("Coupon {0} is not active yet").format(coupon_code))
	if coupon.valid_to and current_date > str(coupon.valid_to):
		frappe.throw(_("Coupon {0} has expired").format(coupon_code))

	if flt(subtotal) < flt(coupon.min_order_amount):
		frappe.throw(
			_("Coupon {0} requires a minimum order of {1}").format(coupon_code, coupon.min_order_amount)
		)

	if coupon.applicable_franchises:
		allowed = {row.franchise for row in coupon.applicable_franchises}
		franchise_name = franchise.name if hasattr(franchise, "name") else franchise
		if franchise_name not in allowed:
			frappe.throw(_("Coupon {0} is not valid at this store").format(coupon_code))

	if coupon.usage_limit_total:
		total_used = frappe.db.count("Saathi Coupon Usage", {"coupon": coupon.name})
		if total_used >= coupon.usage_limit_total:
			frappe.throw(_("Coupon {0} has reached its usage limit").format(coupon_code))

	if coupon.usage_limit_per_customer and customer_mobile:
		used_by_customer = frappe.db.count(
			"Saathi Coupon Usage", {"coupon": coupon.name, "customer_mobile": customer_mobile}
		)
		if used_by_customer >= coupon.usage_limit_per_customer:
			frappe.throw(_("Coupon {0} has already been used").format(coupon_code))

	if coupon.discount_type == "Percentage":
		discount = flt(subtotal) * flt(coupon.value) / 100
		if coupon.max_discount_amount:
			discount = min(discount, flt(coupon.max_discount_amount))
	else:
		discount = flt(coupon.value)

	# Never discount more than the order itself is worth.
	discount = min(discount, flt(subtotal))

	return {"coupon": coupon.name, "discount": discount, "discount_type": coupon.discount_type}


def record_usage(coupon_name, order_name, customer_mobile, discount_amount):
	frappe.get_doc({
		"doctype": "Saathi Coupon Usage",
		"coupon": coupon_name,
		"order": order_name,
		"customer_mobile": customer_mobile,
		"discount_amount": discount_amount,
		"used_at": frappe.utils.now_datetime(),
	}).insert(ignore_permissions=True)
