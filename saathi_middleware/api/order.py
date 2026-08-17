import json
from math import atan2, cos, radians, sin, sqrt

import frappe
from frappe.rate_limiter import rate_limit
from frappe.utils import flt, now_datetime, today

from saathi_middleware.api.cart import resolve_item_code
from saathi_middleware.api.constants import VALID_ORDER_TRANSITIONS
from saathi_middleware.utils import erpnext_client


def _haversine_km(lat1, lon1, lat2, lon2):
	r = 6371
	dlat = radians(lat2 - lat1)
	dlon = radians(lon2 - lon1)
	a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
	return r * 2 * atan2(sqrt(a), sqrt(1 - a))


@frappe.whitelist(allow_guest=True)
@rate_limit(key="get_payment_modes", limit=60, seconds=60)
def get_payment_modes():
	return frappe.get_all(
		"Saathi Payment Mode",
		filters={"is_enabled": 1},
		fields=["name as mode_name", "is_online"],
		order_by="display_order asc",
	)


@frappe.whitelist(allow_guest=True)
@rate_limit(key="get_order_status", limit=60, seconds=60)
def get_order_status(order, customer_mobile):
	"""
	Public, mobile-gated order tracking. The mobile number acts as a shared
	secret — unlike saathimart's fully-open track_order(order_id), a
	sequential order ID alone (SORD-2026-00001) isn't enough to see someone
	else's name/address/order contents.
	"""
	order_doc = frappe.db.get_value(
		"Saathi Order",
		order,
		[
			"name",
			"status",
			"payment_status",
			"payment_mode",
			"customer_mobile",
			"subtotal",
			"delivery_charges",
			"grand_total",
			"placed_at",
		],
		as_dict=True,
	)
	if not order_doc or order_doc.customer_mobile != customer_mobile:
		frappe.throw("Order not found", frappe.DoesNotExistError)
	del order_doc["customer_mobile"]

	order_doc["is_online_payment"] = bool(
		frappe.db.get_value("Saathi Payment Mode", order_doc["payment_mode"], "is_online")
	)
	order_doc["items"] = frappe.get_all(
		"Saathi Order Item",
		filters={"parent": order},
		fields=["item_code", "item_name", "qty", "rate", "amount"],
		order_by="idx asc",
	)
	order_doc["status_timeline"] = _build_status_timeline(order_doc["status"])
	return order_doc


def _build_status_timeline(status):
	"""Blinkit-style status timeline for order tracking."""
	steps = [
		{"status": "Pending",            "label": "Order Placed",     "completed": False},
		{"status": "Confirmed",          "label": "Confirmed",        "completed": False},
		{"status": "Preparing",          "label": "Preparing",        "completed": False},
		{"status": "Out for Delivery",   "label": "Out for Delivery", "completed": False},
		{"status": "Delivered",          "label": "Delivered",        "completed": False},
	]
	cancelled = status == "Cancelled"
	refunded = status == "Refunded"

	status_order = ["Pending", "Confirmed", "Preparing", "Out for Delivery", "Delivered"]
	if cancelled or refunded:
		current_idx = -1
	else:
		try:
			current_idx = status_order.index(status)
		except ValueError:
			current_idx = -1

	for i, step in enumerate(steps):
		if cancelled:
			step["cancelled"] = True
		elif refunded:
			step["refunded"] = True
		elif i < current_idx:
			step["completed"] = True
		elif i == current_idx:
			step["completed"] = True
			step["active"] = True

	if cancelled:
		steps.append({"status": "Cancelled", "label": "Cancelled", "completed": False, "cancelled": True})
	if refunded:
		steps.append({"status": "Refunded", "label": "Refunded", "completed": False, "refunded": True})

	return steps


@frappe.whitelist()
def get_order(order):
	"""Logged-in order detail view — gated by has_order_permission (owner or SM Admin)."""
	doc = frappe.get_doc("Saathi Order", order)
	if not doc.has_permission("read"):
		frappe.throw("Not permitted", frappe.PermissionError)

	data = doc.as_dict()
	data["status_timeline"] = _build_status_timeline(doc.status)
	data["items_summary"] = [
		{
			"item_code": i.item_code,
			"item_name": i.item_name,
			"qty": flt(i.qty),
			"rate": flt(i.rate),
			"amount": flt(i.amount),
		}
		for i in (doc.items or [])
	]
	return data


@frappe.whitelist()
def update_order_status(order, status):
	if "SM Admin" not in frappe.get_roles():
		frappe.throw("Not permitted", frappe.PermissionError)

	doc = frappe.get_doc("Saathi Order", order)
	if status not in VALID_ORDER_TRANSITIONS.get(doc.status, []):
		frappe.throw(f"Invalid status transition: {doc.status} -> {status}")

	doc.status = status
	doc.save(ignore_permissions=True)
	frappe.db.commit()

	from saathi_middleware.api.notifications import create_order_status_notification
	create_order_status_notification(doc, status)

	return {"ok": True, "status": doc.status}


@frappe.whitelist(allow_guest=True)
@rate_limit(key="cancel_order", limit=20, seconds=60)
def cancel_order(order, customer_mobile, reason=None):
	order_doc = frappe.get_doc("Saathi Order", order)
	if order_doc.customer_mobile != customer_mobile:
		frappe.throw("Order not found", frappe.DoesNotExistError)

	if order_doc.order_status == "Cancelled":
		frappe.throw("This order is already cancelled")
	if order_doc.payment_status == "Refunded":
		frappe.throw("This order has already been refunded")

	# Once paid, this becomes a refund request rather than a cancellation -- allowed even after Delivered, since that's exactly the case of a customer who received the goods and wants their money back. Only a not-yet-paid order can no longer be cancelled once
	# delivered (in practice "Delivered" always implies "Paid" already -- the poller only ever sets one alongside the other -- so this mainly guards against a stale read).
	if order_doc.payment_status != "Paid" and order_doc.order_status == "Delivered":
		frappe.throw("This order has already been delivered and can no longer be cancelled")

	if order_doc.erpnext_sales_order:
		franchise = frappe.get_doc("Franchise", order_doc.franchise)
		try:
			_cancel_or_refund_on_franchise(order_doc, franchise)
		except erpnext_client.ERPNextAPIError as e:
			frappe.throw(f"Could not cancel this order right now: {e}")
	else:
		# Never even reached the franchise site yet -- nothing to reverse there.
		order_doc.order_status = "Cancelled"

	order_doc.cancel_reason = reason
	order_doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {"order": order_doc.name, "order_status": order_doc.order_status, "payment_status": order_doc.payment_status}


def _cancel_or_refund_on_franchise(order, franchise):
	if order.erpnext_sales_invoice:
		# Money already collected (prepaid, or COD already billed) -- refund via credit note. The original Sales Order / Delivery Note / Sales Invoice are never touched or cancelled.
		erpnext_client.create_credit_note(franchise, order.erpnext_sales_invoice)
		order.payment_status = "Refunded"
	else:
		# Nothing collected yet -- reverse the stock commitment, in dependency order.
		if order.erpnext_delivery_note:
			erpnext_client.cancel_document(franchise, "Delivery Note", order.erpnext_delivery_note)
		erpnext_client.cancel_document(franchise, "Sales Order", order.erpnext_sales_order)
		if order.payment_status == "COD Pending":
			order.payment_status = "Failed"

	order.order_status = "Cancelled"


@frappe.whitelist(allow_guest=True)
@rate_limit(key="confirm_delivery", limit=20, seconds=60)
def confirm_delivery(order, customer_mobile, amount_collected=None):
	order_doc = frappe.get_doc("Saathi Order", order)
	if order_doc.customer_mobile != customer_mobile:
		frappe.throw("Order not found", frappe.DoesNotExistError)
	if order_doc.order_status not in ("Pushed", "Confirmed"):
		frappe.throw(f"Order is {order_doc.order_status}, cannot confirm delivery")

	if not order_doc.erpnext_sales_invoice:
		# COD, not yet billed -- this is what bills it, instead of middleware finding out later that someone billed it directly on the franchise's own ERPNext.
		franchise = frappe.get_doc("Franchise", order_doc.franchise)
		payment_mode = frappe.get_doc("Saathi Payment Mode", order_doc.payment_mode)
		mode_of_payment = payment_mode.erpnext_mode_of_payment or payment_mode.mode_name
		amount = flt(amount_collected) if amount_collected is not None else order_doc.grand_total
		# COD always creates a Delivery Note at push time (see _attempt_push), which already moved the stock -- this invoice must not move it again.
		order_doc.erpnext_sales_invoice = erpnext_client.create_paid_sales_invoice(
			franchise, order_doc.erpnext_sales_order, mode_of_payment, amount, update_stock=0
		)
		order_doc.payment_status = "Paid"
	# else: prepaid order, already billed and paid at push time -- nothing further to bill.

	order_doc.order_status = "Delivered"
	order_doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {
		"order": order_doc.name,
		"order_status": order_doc.order_status,
		"payment_status": order_doc.payment_status,
	}


def _validate_and_apply_coupon(coupon_code, franchise, customer_mobile, subtotal):
	if not coupon_code:
		return None, 0

	if not frappe.db.exists("Saathi Coupon", coupon_code):
		frappe.throw(f"Coupon {coupon_code} is not valid")
	coupon = frappe.get_doc("Saathi Coupon", coupon_code)
	if not coupon.is_active:
		frappe.throw(f"Coupon {coupon_code} is not valid")

	today = frappe.utils.today()
	if coupon.valid_from and today < str(coupon.valid_from):
		frappe.throw(f"Coupon {coupon_code} is not active yet")
	if coupon.valid_to and today > str(coupon.valid_to):
		frappe.throw(f"Coupon {coupon_code} has expired")

	if flt(subtotal) < flt(coupon.min_order_amount):
		frappe.throw(f"Coupon {coupon_code} requires a minimum order of {coupon.min_order_amount}")

	if coupon.applicable_franchises:
		allowed = {row.franchise for row in coupon.applicable_franchises}
		if franchise.name not in allowed:
			frappe.throw(f"Coupon {coupon_code} is not valid at {franchise.franchise_name}")

	if coupon.usage_limit_total and frappe.db.count("Saathi Coupon Usage", {"coupon": coupon.name}) >= coupon.usage_limit_total:
		frappe.throw(f"Coupon {coupon_code} has reached its usage limit")

	if coupon.usage_limit_per_customer:
		used_by_customer = frappe.db.count(
			"Saathi Coupon Usage", {"coupon": coupon.name, "customer_mobile": customer_mobile}
		)
		if used_by_customer >= coupon.usage_limit_per_customer:
			frappe.throw(f"Coupon {coupon_code} has already been used")

	if coupon.discount_type == "Percentage":
		discount = flt(subtotal) * flt(coupon.value) / 100
		if coupon.max_discount_amount:
			discount = min(discount, flt(coupon.max_discount_amount))
	else:
		discount = flt(coupon.value)

	# Never discount more than the order itself is worth.
	discount = min(discount, flt(subtotal))
	return coupon.name, discount


@frappe.whitelist(allow_guest=True)
@rate_limit(key="create_order", limit=20, seconds=60)
def create_order(**payload):
	franchise, distance_km = _get_serviceable_franchise(payload)
	payment_mode = _get_payment_mode(payload.get("payment_mode"))
	order_items = _validate_and_price_items(franchise, payload.get("items") or [])
	delivery_charges = _calculate_delivery_charges(franchise, distance_km)

	subtotal = sum(flt(item["qty"]) * flt(item["rate"]) for item in order_items)
	coupon_name, discount_amount = _validate_and_apply_coupon(
		payload.get("coupon_code"), franchise, payload.get("customer_mobile"), subtotal
	)

	order = frappe.get_doc(
		{
			"doctype": "Saathi Order",
			"franchise": franchise.name,
			"payment_mode": payment_mode.name,
			"customer_name": payload.get("customer_name"),
			"customer_mobile": payload.get("customer_mobile"),
			"customer_email": payload.get("customer_email"),
			"delivery_address": payload.get("delivery_address"),
			"delivery_city": payload.get("delivery_city"),
			"delivery_latitude": payload.get("delivery_latitude"),
			"delivery_longitude": payload.get("delivery_longitude"),
			"delivery_charges": delivery_charges,
			"coupon_code": coupon_name,
			"discount_amount": discount_amount,
			"placed_at": now_datetime(),
			"coupon_code": payload.get("coupon_code") or "",
			"loyalty_points_redeemed": flt(payload.get("loyalty_points") or 0),
			"items": order_items,
		}
	)
	order.insert(ignore_permissions=True)
	frappe.db.commit()

	if coupon_name:
		try:
			from saathi_middleware.saathi_middleware.doctype.sm_coupon.sm_coupon import increment_coupon_usage
			increment_coupon_usage(coupon_name, user=customer_email, order=order.name)
		except Exception:
			frappe.get_doc(
				{
					"doctype": "Saathi Coupon Usage",
					"coupon": coupon_name,
					"order": order.name,
					"customer_mobile": customer_mobile,
					"discount_amount": discount_amount,
				}
			).insert(ignore_permissions=True)
			frappe.db.commit()

	if payment_mode.is_online:
		return {
			"order": order.name,
			"status": "awaiting_payment",
			"grand_total": order.grand_total,
			"coupon_discount": order.coupon_discount,
			"loyalty_discount": order.loyalty_discount,
			"loyalty_points_earned": order.loyalty_points_earned,
		}

	frappe.enqueue(
		"saathi_middleware.api.order.push_order_job",
		queue="short",
		order_name=order.name,
		enqueue_after_commit=True,
	)
	return {
		"order": order.name,
		"status": "placed",
		"grand_total": order.grand_total,
		"coupon_discount": order.coupon_discount,
		"loyalty_discount": order.loyalty_discount,
		"loyalty_points_earned": order.loyalty_points_earned,
	}


@frappe.whitelist(allow_guest=True)
@rate_limit(key="checkout", limit=20, seconds=60)
def checkout(session_id, customer_name, customer_mobile, delivery_address,
             payment_mode, delivery_city=None, delivery_latitude=None,
             delivery_longitude=None, delivery_charges=0, coupon_code=None,
             loyalty_points=0, customer_email=None):
	cart = frappe.db.get_value(
		"SM Cart", {"session_id": session_id, "status": "Active"}, "name"
	)
	if not cart:
		frappe.throw("Cart not found or already checked out")

	cart_doc = frappe.get_doc("SM Cart", cart)
	if not cart_doc.items:
		frappe.throw("Cart is empty")

	payment = _get_payment_mode(payment_mode)
	franchise = _get_serviceable_franchise({
		"franchise": cart_doc.items[0].franchise if cart_doc.items else None,
		"delivery_latitude": delivery_latitude,
		"delivery_longitude": delivery_longitude,
	})

	if coupon_code:
		from saathi_middleware.saathi_middleware.doctype.sm_coupon.sm_coupon import validate_coupon
		validate_coupon(coupon_code, flt(cart_doc.subtotal or 0), user=customer_email, franchise=franchise.name)

	order_items = []
	for item in cart_doc.items:
		order_items.append({
			"item_code": resolve_item_code(item.product, item.franchise),
			"item_name": item.product_name,
			"franchise": item.franchise or franchise.name,
			"qty": flt(item.qty),
			"rate": flt(item.rate),
		})

	order = frappe.get_doc({
		"doctype": "Saathi Order",
		"franchise": franchise.name,
		"payment_mode": payment.name,
		"customer_name": customer_name,
		"customer_mobile": customer_mobile,
		"customer_email": customer_email or "",
		"delivery_address": delivery_address,
		"delivery_city": delivery_city or "",
		"delivery_latitude": flt(delivery_latitude) if delivery_latitude else None,
		"delivery_longitude": flt(delivery_longitude) if delivery_longitude else None,
		"delivery_charges": flt(delivery_charges) or 0,
		"placed_at": now_datetime(),
		"coupon_code": coupon_code or "",
		"loyalty_points_redeemed": flt(loyalty_points) or 0,
		"items": order_items,
	})
	order.insert(ignore_permissions=True)
	frappe.db.commit()

	if order.coupon_code:
		try:
			from saathi_middleware.saathi_middleware.doctype.sm_coupon.sm_coupon import increment_coupon_usage
			increment_coupon_usage(order.coupon_code, user=customer_email, order=order.name)
		except Exception:
			pass

	cart_doc.db_set("status", "CheckedOut")

	if payment.is_online:
		return {
			"order": order.name,
			"status": "awaiting_payment",
			"grand_total": order.grand_total,
			"coupon_discount": order.coupon_discount,
			"loyalty_discount": order.loyalty_discount,
			"loyalty_points_earned": order.loyalty_points_earned,
		}

	frappe.enqueue(
		"saathi_middleware.api.order.push_order_job",
		queue="short",
		order_name=order.name,
		enqueue_after_commit=True,
	)
	return {
		"order": order.name,
		"status": "placed",
		"grand_total": order.grand_total,
		"coupon_discount": order.coupon_discount,
		"loyalty_discount": order.loyalty_discount,
		"loyalty_points_earned": order.loyalty_points_earned,
	}


@frappe.whitelist(allow_guest=True)
@rate_limit(key="calculate_cart_totals", limit=60, seconds=60)
def calculate_cart_totals(session_id, coupon_code=None, loyalty_points=0, delivery_charges=0):
	"""
	Preview what checkout() would actually charge for the current cart —
	same subtotal/coupon/loyalty pipeline, but read-only: no order is
	created, no coupon usage incremented, no points deducted. Safe to call
	on every keystroke while the customer types a coupon code or a
	points-to-redeem amount on the cart page.
	"""
	cart = frappe.db.get_value(
		"SM Cart", {"session_id": session_id, "status": "Active"}, "name"
	)
	if not cart:
		frappe.throw("Cart not found or already checked out")

	cart_doc = frappe.get_doc("SM Cart", cart)
	if not cart_doc.items:
		frappe.throw("Cart is empty")

	subtotal = sum(flt(item.qty) * flt(item.rate) for item in cart_doc.items)
	delivery_charges = flt(delivery_charges) or 0
	user = frappe.session.user if frappe.session.user != "Guest" else None

	coupon_discount = 0.0
	coupon_error = None
	applied_coupon_code = None
	if coupon_code:
		franchise = cart_doc.items[0].franchise
		try:
			from saathi_middleware.saathi_middleware.doctype.sm_coupon.sm_coupon import validate_coupon
			result = validate_coupon(coupon_code, subtotal, user=user, franchise=franchise)
			coupon_discount = flt(result.get("discount", 0))
			if result.get("free_delivery"):
				delivery_charges = 0
			applied_coupon_code = coupon_code
		except frappe.ValidationError as e:
			coupon_error = str(e)

	loyalty_discount = 0.0
	loyalty_points_applied = 0.0
	loyalty_error = None
	loyalty_points = flt(loyalty_points)
	if loyalty_points:
		if not user:
			loyalty_error = "Login required to redeem points"
		else:
			from saathi_middleware.api.loyalty import calculate_redemption_discount
			result = calculate_redemption_discount(user, loyalty_points, subtotal)
			if result.get("ok"):
				loyalty_discount = flt(result.get("discount", 0))
				loyalty_points_applied = flt(result.get("points_used", 0))
			else:
				loyalty_error = result.get("error")

	grand_total = subtotal + delivery_charges - coupon_discount - loyalty_discount

	return {
		"subtotal": round(subtotal, 2),
		"delivery_charges": round(delivery_charges, 2),
		"coupon_code": applied_coupon_code,
		"coupon_discount": round(coupon_discount, 2),
		"coupon_error": coupon_error,
		"loyalty_points_requested": loyalty_points,
		"loyalty_points_applied": round(loyalty_points_applied, 2),
		"loyalty_discount": round(loyalty_discount, 2),
		"loyalty_error": loyalty_error,
		"grand_total": round(grand_total, 2),
	}


@frappe.whitelist()
def list_orders(customer_mobile=None, page=1, page_size=20):
	page = max(1, int(page))
	page_size = min(100, max(1, int(page_size)))

	# Staff may look up any customer's orders by mobile for support; regular
	# customers are scoped to their own session — list_orders uses
	# frappe.get_list (permission-checked), but the has_order_permission hook
	# only gates single-doc reads, not list queries, so accepting an
	# arbitrary customer_mobile here would let any logged-in customer browse
	# anyone else's order history just by guessing a mobile number.
	if {"SM Admin", "SM Vendor"} & set(frappe.get_roles()):
		if not customer_mobile:
			frappe.throw("customer_mobile is required")
		filters = {"customer_mobile": customer_mobile}
	else:
		if frappe.session.user == "Guest":
			frappe.throw("Not logged in", frappe.PermissionError)
		filters = {"customer_email": frappe.session.user}

	orders = frappe.get_list(
		"Saathi Order",
		filters=filters,
		fields=["name", "customer_name", "status", "payment_status", "payment_mode",
		        "grand_total", "creation", "sync_status"],
		limit_start=(page - 1) * page_size,
		limit=page_size,
		order_by="creation desc",
	)

	for o in orders:
		o["item_count"] = frappe.db.count("Saathi Order Item", {"parent": o["name"]})

	return orders


def _get_payment_mode(mode_name):
	if not mode_name:
		frappe.throw("payment_mode is required")
	mode = frappe.db.get_value(
		"Saathi Payment Mode", mode_name, ["name", "is_enabled", "is_online"], as_dict=True
	)
	if not mode or not mode.is_enabled:
		frappe.throw(f"Payment mode {mode_name} is not available")
	return mode


def _get_serviceable_franchise(payload):
	site_code = payload.get("franchise")
	if not site_code:
		frappe.throw("franchise is required")
	franchise = frappe.get_doc("Franchise", site_code)
	if franchise.status != "Active":
		frappe.throw(f"Franchise {site_code} is not active")

	distance_km = None
	lat = payload.get("delivery_latitude")
	lng = payload.get("delivery_longitude")
	if lat is not None and lng is not None and franchise.latitude and franchise.longitude:
		distance_km = _haversine_km(float(lat), float(lng), franchise.latitude, franchise.longitude)
		if distance_km > (franchise.serviceable_radius_km or 0):
			frappe.throw(f"Delivery address is outside {franchise.franchise_name}'s serviceable area")

	return franchise, distance_km


def _calculate_delivery_charges(franchise, distance_km):
	base = frappe.utils.flt(franchise.delivery_base_charge)
	if distance_km is None:
		return base
	free_km = frappe.utils.flt(franchise.free_delivery_upto_km)
	chargeable_km = max(0.0, distance_km - free_km)
	return base + chargeable_km * frappe.utils.flt(franchise.delivery_per_km_rate)


def _validate_and_price_items(franchise, requested_items):
	if not requested_items:
		frappe.throw("At least one item is required")

	order_items = []
	for row in requested_items:
		item_code = row.get("item_code")
		qty = frappe.utils.flt(row.get("qty"))
		if not item_code or qty <= 0:
			frappe.throw("Each item needs a valid item_code and qty")

		saathi_item = frappe.db.get_value(
			"Saathi Item",
			f"{franchise.name}-{item_code}",
			["item_name", "price", "stock_qty", "is_active"],
			as_dict=True,
		)
		if not saathi_item or not saathi_item.is_active:
			frappe.throw(f"{item_code} is not available at {franchise.franchise_name}")
		if saathi_item.stock_qty is not None and qty > saathi_item.stock_qty:
			frappe.throw(f"Only {saathi_item.stock_qty} of {item_code} in stock")

		order_items.append(
			{
				"item_code": item_code,
				"item_name": saathi_item.item_name,
				"franchise": franchise.name,
				"qty": qty,
				"rate": saathi_item.price,
			}
		)
	return order_items


def push_order_job(order_name):
	order = frappe.get_doc("Saathi Order", order_name)
	log = _get_or_create_log(order_name)
	_attempt_push(order, log)


def _get_or_create_log(order_name):
	if frappe.db.exists("Saathi Order Sync Log", order_name):
		return frappe.get_doc("Saathi Order Sync Log", order_name)
	log = frappe.get_doc(
		{"doctype": "Saathi Order Sync Log", "order": order_name, "status": "Pending"}
	).insert(ignore_permissions=True)
	frappe.db.commit()
	return log


def _attempt_push(order, log):
	franchise = frappe.get_doc("Franchise", order.franchise)
	payment_mode = frappe.get_doc("Saathi Payment Mode", order.payment_mode)

	log.attempts += 1
	log.last_attempt = now_datetime()

	try:
		if not order.erpnext_customer:
			order.erpnext_customer = erpnext_client.upsert_customer(
				franchise, order.customer_name, order.customer_mobile, order.customer_email
			)
			order.save(ignore_permissions=True)
			frappe.db.commit()

		if not order.erpnext_address:
			order.erpnext_address = erpnext_client.upsert_address(
				franchise, order.erpnext_customer, order.delivery_address, order.delivery_city
			)
			order.save(ignore_permissions=True)
			frappe.db.commit()

		if not order.erpnext_sales_order:
			items = [{"item_code": row.item_code, "qty": row.qty, "rate": row.rate} for row in order.items]
			order.erpnext_sales_order = erpnext_client.create_sales_order(
				franchise,
				order.erpnext_customer,
				order.erpnext_address,
				items,
				today(),
				order.delivery_charges,
				order.discount_amount,
			)
			order.save(ignore_permissions=True)
			frappe.db.commit()

		if payment_mode.is_online:
			if not order.erpnext_sales_invoice:
				mode_of_payment = payment_mode.erpnext_mode_of_payment or payment_mode.mode_name
				order.erpnext_sales_invoice = erpnext_client.create_paid_sales_invoice(
					franchise, order.erpnext_sales_order, mode_of_payment, order.grand_total
				)
				order.payment_status = "Paid"
		else:
			if not order.erpnext_delivery_note:
				order.erpnext_delivery_note = erpnext_client.create_delivery_note(
					franchise, order.erpnext_sales_order
				)
				order.payment_status = "COD Pending"

		order.sync_status = "Success"
		order.save(ignore_permissions=True)

		log.status = "Success"
		log.error = None
		log.response = json.dumps(
			{
				"erpnext_customer": order.erpnext_customer,
				"erpnext_address": order.erpnext_address,
				"erpnext_sales_order": order.erpnext_sales_order,
				"erpnext_sales_invoice": order.erpnext_sales_invoice,
				"erpnext_delivery_note": order.erpnext_delivery_note,
			}
		)
	except Exception:
		order.sync_status = "Failed"
		order.save(ignore_permissions=True)
		log.status = "Failed"
		log.error = frappe.get_traceback()[:4000]
		frappe.log_error(title=f"Saathi Order push failed: {order.name}", message=frappe.get_traceback())

	frappe.db.commit()
	log.save(ignore_permissions=True)
	frappe.db.commit()


def retry_failed_order_syncs():
	names = frappe.get_all(
		"Saathi Order Sync Log",
		filters={"status": ["in", ["Pending", "Failed"]], "attempts": ["<", 5]},
		pluck="order",
		limit=100,
	)
	for order_name in names:
		order = frappe.get_doc("Saathi Order", order_name)
		log = frappe.get_doc("Saathi Order Sync Log", order_name)
		_attempt_push(order, log)


def poll_franchise_order_status():
	# Reads ERPNext's own status fields on the franchise site -- no franchise-side code involved. Delivered/Cancelled/Paid/Refunded/Failed only ever get set here, never by the push job, because they describe events (staff billing, staff cancelling) that happen on the franchise's own site after the push already completed.
	names = frappe.get_all(
		"Saathi Order",
		filters={"order_status": ["in", ["Pushed", "Confirmed"]]},
		pluck="name",
		limit=200,
	)
	for order_name in names:
		try:
			_poll_single_order(order_name)
		except Exception:
			frappe.log_error(
				title=f"Saathi Order status poll failed: {order_name}", message=frappe.get_traceback()
			)


def _poll_single_order(order_name):
	order = frappe.get_doc("Saathi Order", order_name)
	if not order.erpnext_sales_order:
		return
	franchise = frappe.get_doc("Franchise", order.franchise)

	if order.erpnext_sales_invoice:
		invoice_status = erpnext_client.get_document_status(franchise, "Sales Invoice", order.erpnext_sales_invoice)
		if invoice_status == "Paid":
			order.payment_status = "Paid"
		elif invoice_status in ("Return", "Credit Note Issued"):
			order.payment_status = "Refunded"

	so_status = erpnext_client.get_document_status(franchise, "Sales Order", order.erpnext_sales_order)
	if so_status in ("Cancelled", "Closed"):
		if order.payment_status == "COD Pending":
			# Goods were never paid for and the order is being abandoned -- COD delivery failed.
			order.payment_status = "Failed"
		order.order_status = "Cancelled"
	elif so_status == "Completed":
		# Fully delivered AND fully billed. For COD this is staff creating the Sales Invoice once cash is collected (a standard ERPNext action on their own site, not something middleware initiated) -- so go find that invoice and cache its name. Without this, a later cancel_order call would see erpnext_sales_invoice empty, wrongly conclude
		# nothing was paid, and try to cancel a Sales Order that already has a paid invoice against it.
		if not order.erpnext_sales_invoice:
			order.erpnext_sales_invoice = erpnext_client.find_sales_invoice_for_order(
				franchise, order.erpnext_sales_order
			)
		order.order_status = "Delivered"
		if order.payment_status != "Refunded":
			order.payment_status = "Paid"

	order.save(ignore_permissions=True)
	frappe.db.commit()
