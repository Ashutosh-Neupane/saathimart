import json
from math import atan2, cos, radians, sin, sqrt

import frappe
from frappe.rate_limiter import rate_limit
from frappe.utils import now_datetime, today

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
	order_doc = frappe.db.get_value(
		"Saathi Order",
		order,
		[
			"name",
			"order_status",
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
	# Same response for "doesn't exist" and "web doesn't match" -- an order ID alone must never be enough to confirm it exists or see whose it is.
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
	return order_doc


@frappe.whitelist(allow_guest=True)
@rate_limit(key="create_order", limit=20, seconds=60)
def create_order(**payload):
	franchise = _get_serviceable_franchise(payload)
	payment_mode = _get_payment_mode(payload.get("payment_mode"))
	order_items = _validate_and_price_items(franchise, payload.get("items") or [])

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
			"delivery_charges": payload.get("delivery_charges") or 0,
			"placed_at": now_datetime(),
			"items": order_items,
		}
	)
	order.insert(ignore_permissions=True)
	frappe.db.commit()

	if payment_mode.is_online:
		# Online payment isn't wired up yet: order stays Placed/Pending until a (future) payment confirmation flips payment_status to Paid and pushes it.
		return {"order": order.name, "status": "awaiting_payment", "grand_total": order.grand_total}

	frappe.enqueue(
		"saathi_middleware.api.order.push_order_job",
		queue="short",
		order_name=order.name,
		enqueue_after_commit=True,
	)
	return {"order": order.name, "status": "placed", "grand_total": order.grand_total}


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

	lat = payload.get("delivery_latitude")
	lng = payload.get("delivery_longitude")
	if lat is not None and lng is not None and franchise.latitude and franchise.longitude:
		distance_km = _haversine_km(float(lat), float(lng), franchise.latitude, franchise.longitude)
		if distance_km > (franchise.serviceable_radius_km or 0):
			frappe.throw(f"Delivery address is outside {franchise.franchise_name}'s serviceable area")

	return franchise


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
				franchise, order.erpnext_customer, order.erpnext_address, items, today()
			)
			order.order_status = "Pushed"
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

		order.order_status = "Confirmed"
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
