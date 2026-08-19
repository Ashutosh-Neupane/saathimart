"""
Payment gateway integration - eSewa v2. Adapted from saathimart's
api/payments.py for saathi_middleware's model: a single Saathi Order
(not a multi-vendor Order), payment_mode is a Link to Saathi Payment
Mode (is_online flag decides whether a gateway is needed at all) rather
than a Select of method names, so there's no `method` argument here -
eSewa is the only online gateway (Khalti was dropped by saathimart
itself as untested; same call here).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from urllib.parse import urlencode

import frappe
from frappe.utils import flt, now_datetime
import requests


# ── Settings helpers ──────────────────────────────────────────

def _settings():
	return frappe.get_single("Saathi Settings")


def _get_password(settings, field):
	try:
		val = settings.get_password(field)
	except Exception:
		val = None
	return (val or getattr(settings, field, "") or "").strip()


def _base_url():
	try:
		from frappe.utils import get_url
		return get_url().rstrip("/")
	except Exception:
		return f"https://{frappe.local.site}"


def _api_method_url(method_name):
	return f"{_base_url()}/api/method/saathi_middleware.api.payments.{method_name}"


def _frontend_base():
	s = _settings()
	return (getattr(s, "payment_portal_base_url", None) or _base_url()).rstrip("/")


def _frontend_redirect(path, **params):
	query = urlencode({k: v for k, v in params.items() if v not in (None, "")})
	url = f"{_frontend_base()}{path}"
	return f"{url}?{query}" if query else url


def _redirect(url):
	"""Send the shopper's browser to `url`. These callback endpoints are hit
	via a top-level browser redirect from eSewa, never fetch/XHR."""
	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = url


# ── Initiate ──────────────────────────────────────────

@frappe.whitelist(allow_guest=True)
def initiate_payment(order):
	s = _settings()
	order_doc = frappe.get_doc("Saathi Order", order)
	if order_doc.payment_status == "Paid":
		frappe.throw("This order is already paid.")

	payment_mode = frappe.get_doc("Saathi Payment Mode", order_doc.payment_mode)
	if not payment_mode.is_online:
		frappe.throw("This order's payment mode does not require online payment.")
	if not getattr(s, "enable_esewa", 1):
		frappe.throw("eSewa is not enabled.")

	amount = flt(order_doc.grand_total)
	if amount <= 0:
		frappe.throw("Order amount must be greater than zero.")

	sandbox = bool(getattr(s, "payment_sandbox_mode", 1))
	return _initiate_esewa(s, order_doc.name, amount, sandbox)


def _initiate_esewa(s, order_name, amount, sandbox):
	merchant_code = getattr(s, "esewa_merchant_code", None) or "EPAYTEST"
	secret_key = _get_password(s, "esewa_secret_key")
	if not secret_key:
		frappe.throw("eSewa Secret Key not configured in Settings.")

	# eSewa rejects a transaction_uuid it has already seen ("Duplicate
	# transaction UUID") — the order name alone isn't enough once a
	# shopper retries after a cancelled/failed attempt (checkout/review's
	# "Try Again" re-initiates for the *same* Saathi Order). Suffixing
	# with a timestamp makes every attempt unique to eSewa while staying
	# reversible: esewa_success/esewa_failure strip it via rsplit("-", 1)
	# to recover the real order name.
	transaction_uuid = f"{order_name}-{int(now_datetime().timestamp())}"

	s_total = f"{amount:.2f}".rstrip("0").rstrip(".")
	message = f"total_amount={s_total},transaction_uuid={transaction_uuid},product_code={merchant_code}"
	signature = base64.b64encode(
		hmac.new(secret_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
	).decode("utf-8")

	# Persisted so verify_esewa_status (manual check + the 10-min cron poll)
	# queries eSewa about the *actual* attempt it accepted, not the bare
	# order name it never submitted.
	frappe.db.set_value("Saathi Order", order_name, "esewa_transaction_uid", transaction_uuid)
	frappe.db.commit()

	gw_base = (
		getattr(s, "esewa_base_url", None)
		or ("https://rc-epay.esewa.com.np" if sandbox else "https://epay.esewa.com.np")
	).rstrip("/")

	return {
		"gateway": "eSewa",
		"sandbox": sandbox,
		"payment_url": f"{gw_base}/api/epay/main/v2/form",
		"fields": {
			"amount": s_total,
			"tax_amount": "0",
			"total_amount": s_total,
			"transaction_uuid": transaction_uuid,
			"product_code": merchant_code,
			"product_service_charge": "0",
			"product_delivery_charge": "0",
			"success_url": _api_method_url("esewa_success"),
			"failure_url": _api_method_url("esewa_failure"),
			"signed_field_names": "total_amount,transaction_uuid,product_code",
			"signature": signature,
		},
		"order": order_name,
	}


# ── Verify ──────────────────────────────────────────

def _verify_esewa_signature(payload):
	s = _settings()
	secret_key = _get_password(s, "esewa_secret_key")
	if not secret_key:
		return False, "eSewa Secret Key not configured"

	signed_fields = payload.get("signed_field_names", "")
	if not signed_fields:
		return False, "Missing signed_field_names"

	fields = [f.strip() for f in signed_fields.split(",") if f.strip() and f.strip() in payload]
	if not fields:
		return False, "No signed fields present in payload"

	message = ",".join(f"{f}={payload[f]}" for f in fields)
	expected = base64.b64encode(
		hmac.new(secret_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
	).decode("utf-8")

	if payload.get("signature") != expected:
		frappe.logger().warning(
			f"[eSewa] Sig mismatch. expected={expected} got={payload.get('signature')} msg={message}"
		)
		return False, "Invalid payment signature"

	return True, None


# ── Callbacks ──────────────────────────────────────────
#
# eSewa redirects the shopper's browser here (success_url/failure_url above)
# - never called via fetch/XHR. So each one verifies the payment, updates
# the order, and 302s the browser to the storefront's own success/failure
# page (Settings > Payment Portal Base URL) rather than returning JSON.

def _order_name_from_transaction_uuid(transaction_uuid):
	"""transaction_uuid is "{order_name}-{unix_ts}" (see _initiate_esewa) —
	rsplit on the last "-" instead of a plain lookup, since Saathi Order
	names already contain dashes (e.g. "SORD-2026-00001")."""
	if not transaction_uuid:
		return None
	order_name = transaction_uuid.rsplit("-", 1)[0]
	return order_name if frappe.db.exists("Saathi Order", order_name) else None


@frappe.whitelist(allow_guest=True)
def esewa_success(data=None, **kwargs):
	payload = _decode_esewa_data(data, kwargs)
	if not payload:
		return _redirect(_frontend_redirect("/payment/failure", error="No payment data"))

	ok, err = _verify_esewa_signature(payload)
	if not ok:
		return _redirect(_frontend_redirect("/payment/failure", error=err))

	if payload.get("status", "").upper() != "COMPLETE":
		return _redirect(_frontend_redirect(
			"/payment/failure", error=f"Payment status: {payload.get('status')}",
		))

	transaction_uuid = payload.get("transaction_uuid")
	order_name = _order_name_from_transaction_uuid(transaction_uuid)
	if not order_name:
		return _redirect(_frontend_redirect("/payment/failure", error="Order not found"))

	amount = flt(payload.get("total_amount", 0))
	order_doc = frappe.get_doc("Saathi Order", order_name)
	if abs(amount - flt(order_doc.grand_total)) > 1:
		return _redirect(_frontend_redirect(
			"/payment/failure", order=order_name, error="Amount mismatch",
		))

	_mark_order_paid(
		order_name,
		gateway="eSewa",
		reference=payload.get("transaction_code", ""),
		transaction_uid=transaction_uuid,
		amount=amount,
	)
	return _redirect(_frontend_redirect("/payment/success", order=order_name, gateway="eSewa"))


@frappe.whitelist(allow_guest=True)
def esewa_failure(data=None, **kwargs):
	payload = _decode_esewa_data(data, kwargs)
	order_name = _order_name_from_transaction_uuid((payload or {}).get("transaction_uuid")) or kwargs.get("order")
	if order_name and frappe.db.exists("Saathi Order", order_name):
		_mark_order_failed(order_name, gateway="eSewa", message="Payment cancelled or failed")
	return _redirect(_frontend_redirect(
		"/payment/failure", order=order_name, error="Payment cancelled or failed",
	))


@frappe.whitelist(allow_guest=True)
def verify_esewa_status(order):
	"""Poll eSewa's transaction status API directly - used by cron to catch
	lost callbacks, and safe to call manually to double check a payment."""
	s = _settings()
	order_doc = frappe.get_doc("Saathi Order", order)
	if order_doc.payment_status == "Paid":
		return {"status": "Paid"}

	# eSewa's status API is keyed by the exact transaction_uuid an
	# initiate call actually used (order_name-timestamp), not the bare
	# order name — see _initiate_esewa. No attempt yet means nothing to
	# check.
	transaction_uuid = order_doc.esewa_transaction_uid
	if not transaction_uuid:
		return {"status": "not_initiated"}

	merchant_code = getattr(s, "esewa_merchant_code", None) or "EPAYTEST"
	sandbox = bool(getattr(s, "payment_sandbox_mode", 1))
	gw_base = (
		getattr(s, "esewa_base_url", None)
		or ("https://rc-epay.esewa.com.np" if sandbox else "https://epay.esewa.com.np")
	).rstrip("/")

	try:
		resp = requests.get(
			f"{gw_base}/api/epay/transaction/status/",
			params={
				"product_code": merchant_code,
				"total_amount": str(flt(order_doc.grand_total)),
				"transaction_uuid": transaction_uuid,
			},
			timeout=20,
		)
		data = resp.json()
		if resp.ok and data.get("status") == "COMPLETE":
			_mark_order_paid(order, gateway="eSewa", reference=data.get("ref_id", ""),
			                 transaction_uid=transaction_uuid, amount=flt(order_doc.grand_total))
			return {"status": "Paid", "ref_id": data.get("ref_id")}
		return {"status": data.get("status", "unknown")}
	except Exception as e:
		return {"status": "error", "error": str(e)}


# ── Internal helpers ──────────────────────────────────────────

def _decode_esewa_data(data, kwargs):
	if data:
		try:
			return json.loads(base64.b64decode(data))
		except Exception:
			return None
	if kwargs.get("signed_field_names"):
		return kwargs
	return None


def _mark_order_paid(order_name, gateway, reference, transaction_uid, amount):
	"""Update Saathi Order + create Payment Log, bump status to Confirmed,
	and fire the same confirmation email/notification a COD order gets on
	placement. Idempotent - a lost-callback poll landing after the direct
	callback already processed it is a no-op."""
	if frappe.db.get_value("Saathi Order", order_name, "payment_status") == "Paid":
		return

	frappe.db.set_value("Saathi Order", order_name, {
		"payment_status": "Paid",
		"payment_reference": reference,
		"esewa_transaction_uid": transaction_uid,
	})

	log = frappe.new_doc("Payment Log")
	log.order = order_name
	log.gateway = gateway
	log.status = "Success"
	log.amount = amount
	log.reference = reference
	log.transaction_uid = transaction_uid
	log.insert(ignore_permissions=True)
	frappe.db.commit()

	from saathi_middleware.api.constants import VALID_ORDER_TRANSITIONS
	order_doc = frappe.get_doc("Saathi Order", order_name)
	if "Confirmed" in VALID_ORDER_TRANSITIONS.get(order_doc.status, []):
		order_doc.status = "Confirmed"
		order_doc.save(ignore_permissions=True)
		frappe.db.commit()
		from saathi_middleware.api.notifications import create_order_status_notification
		create_order_status_notification(order_doc, "Confirmed")

	from saathi_middleware.api.order import _send_order_confirmation_email
	_send_order_confirmation_email(order_doc)

	# create_order()/checkout() only enqueue the ERPNext franchise-sync job
	# for COD/offline orders (nothing to sync yet for an online order until
	# payment actually clears) — this is the online-order equivalent of that
	# same enqueue, now that payment has cleared.
	frappe.enqueue(
		"saathi_middleware.api.order.push_order_job",
		queue="short",
		order_name=order_name,
		enqueue_after_commit=True,
	)

	# esewa_success/verify_esewa_status both return a redirect or a plain
	# dict rather than the normal whitelisted-JSON response, which is what
	# usually triggers Frappe's implicit end-of-request commit — without
	# this, the notification insert and the queued confirmation email
	# above were silently dropped on rollback while the order/Payment Log
	# writes above (each already explicitly committed) persisted.
	frappe.db.commit()


def _mark_order_failed(order_name, gateway, message):
	frappe.db.set_value("Saathi Order", order_name, {"payment_status": "Failed"})

	log = frappe.new_doc("Payment Log")
	log.order = order_name
	log.gateway = gateway
	log.status = "Failed"
	log.amount = 0
	log.notes = message or ""
	log.insert(ignore_permissions=True)
	frappe.db.commit()


def poll_pending_esewa_orders():
	"""Cron every 10 min - catch payments where eSewa's redirect callback
	never reached us (browser closed mid-flow, network drop, ...)."""
	pending = frappe.get_list(
		"Saathi Order",
		filters={
			"payment_status": "Pending",
			"creation": ["<", frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-10)],
		},
		fields=["name", "payment_mode"],
		limit=20,
		ignore_permissions=True,
	)
	for row in pending:
		is_online = frappe.db.get_value("Saathi Payment Mode", row.payment_mode, "is_online")
		if not is_online:
			continue
		try:
			verify_esewa_status(row.name)
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"eSewa poll failed for {row.name}")
