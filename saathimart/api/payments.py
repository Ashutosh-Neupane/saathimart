"""
Payment gateway integration — eSewa v2 + Khalti.
Adapted from trevo_ecommerce. No ERPNext dependency.
All accounting is against SM Order, not Sales Order.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from urllib.parse import urlencode

import frappe
from frappe import _
from frappe.utils import flt, today
import requests


# ── Settings helpers ──────────────────────────────────────────────────────────

def _settings():
    return frappe.get_single("Settings")


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
    """URL of one of this module's whitelisted methods, for gateways to redirect to."""
    return f"{_base_url()}/api/method/saathimart.api.payments.{method_name}"


def _frontend_base():
    """Base URL of the storefront that consumes this API — where shoppers land
    after a payment gateway redirect has been verified. Falls back to this
    site's own URL if no storefront is configured."""
    s = _settings()
    return (getattr(s, "payment_portal_base_url", None) or _base_url()).rstrip("/")


def _frontend_redirect(path, **params):
    query = urlencode({k: v for k, v in params.items() if v not in (None, "")})
    url = f"{_frontend_base()}{path}"
    return f"{url}?{query}" if query else url


def _redirect(url):
    """Send the shopper's browser to `url`. Used by the gateway-facing
    callback endpoints, which are hit via a top-level browser redirect
    from eSewa/Khalti — never via fetch/XHR."""
    frappe.local.response["type"] = "redirect"
    frappe.local.response["location"] = url


# ── Initiate ──────────────────────────────────────────────────────────────────

@frappe.whitelist(allow_guest=True)
def initiate_payment(method, order_id, customer_info=None):
    """
    Initiate eSewa or Khalti payment for an SM Order.
    Returns gateway payload the frontend uses to redirect.
    """
    method = (method or "").lower()
    s = _settings()
    sandbox = bool(getattr(s, "payment_sandbox_mode", 1))

    order = frappe.get_doc("Order", order_id)
    if order.payment_status == "Paid":
        frappe.throw(_("This order is already paid."))

    amount = flt(order.grand_total)
    if amount <= 0:
        frappe.throw(_("Order amount must be greater than zero."))

    if method == "esewa":
        if not getattr(s, "enable_esewa", 1):
            frappe.throw(_("eSewa is not enabled."))
        return _initiate_esewa(s, order_id, amount, sandbox)

    if method == "khalti":
        if not getattr(s, "enable_khalti", 0):
            frappe.throw(_("Khalti is not enabled."))
        return _initiate_khalti(s, order_id, amount, sandbox, customer_info or {})

    frappe.throw(_("Unsupported payment method: {0}").format(method))


def _initiate_esewa(s, order_id, amount, sandbox):
    merchant_code = getattr(s, "esewa_merchant_code", None) or "EPAYTEST"
    secret_key = _get_password(s, "esewa_secret_key")
    if not secret_key:
        frappe.throw(_("eSewa Secret Key not configured in SM Settings."))

    # Use order_id as transaction_uuid — store it on the order for callback lookup
    frappe.db.set_value("Order", order_id, "esewa_transaction_uid", order_id)

    s_total = f"{amount:.2f}".rstrip("0").rstrip(".")
    message = f"total_amount={s_total},transaction_uuid={order_id},product_code={merchant_code}"
    signature = base64.b64encode(
        hmac.new(
            secret_key.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).digest()
    ).decode("utf-8")

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
            "transaction_uuid": order_id,
            "product_code": merchant_code,
            "product_service_charge": "0",
            "product_delivery_charge": "0",
            "success_url": _api_method_url("esewa_success"),
            "failure_url": _api_method_url("esewa_failure"),
            "signed_field_names": "total_amount,transaction_uuid,product_code",
            "signature": signature,
        },
        "order_id": order_id,
    }


def _initiate_khalti(s, order_id, amount, sandbox, customer_info):
    secret_key = _get_password(s, "khalti_secret_key")
    if not secret_key:
        frappe.throw(_("Khalti Secret Key not configured in SM Settings."))
    if secret_key.lower().startswith("key "):
        secret_key = secret_key.split(" ", 1)[1].strip()

    return_url = f"{_api_method_url('khalti_callback')}?order_id={order_id}"

    gw_base = (
        getattr(s, "khalti_base_url", None)
        or ("https://dev.khalti.com/api/v2" if sandbox else "https://khalti.com/api/v2")
    ).rstrip("/")

    payload = {
        "return_url": return_url,
        "website_url": _base_url(),
        "amount": int(amount * 100),
        "purchase_order_id": order_id,
        "purchase_order_name": f"SaathiMart Order {order_id}",
        "customer_info": {
            "name": customer_info.get("name", "Customer"),
            "email": customer_info.get("email", "customer@example.com"),
            "phone": customer_info.get("phone", "9800000000"),
        },
    }

    try:
        resp = requests.post(
            f"{gw_base}/epayment/initiate/",
            headers={"Authorization": f"key {secret_key}", "Content-Type": "application/json"},
            data=json.dumps(payload),
            timeout=20,
        )
        data = resp.json()
        if not resp.ok:
            frappe.throw(_("Khalti initiation failed: {0}").format(data.get("detail", resp.text)))
        return {
            "gateway": "Khalti",
            "sandbox": sandbox,
            "payment_url": data.get("payment_url"),
            "pidx": data.get("pidx"),
            "order_id": order_id,
        }
    except frappe.ValidationError:
        raise
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Khalti Initiation Error")
        frappe.throw(_("Failed to initiate Khalti payment. Please try again."))


# ── Verify ────────────────────────────────────────────────────────────────────

def _verify_esewa_signature(payload):
    """Verify eSewa v2 HMAC-SHA256 signature. Returns (ok, error_msg)."""
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
        hmac.new(
            secret_key.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).digest()
    ).decode("utf-8")

    if payload.get("signature") != expected:
        frappe.logger().warning(
            f"[eSewa] Sig mismatch. expected={expected} got={payload.get('signature')} msg={message}"
        )
        return False, "Invalid payment signature"

    return True, None


# ── Callbacks ─────────────────────────────────────────────────────────────────
#
# These three endpoints are the URLs the gateways themselves redirect the
# shopper's browser to (see success_url/failure_url/return_url above) — they
# are never called via fetch/XHR from a frontend. So instead of returning
# JSON, each one verifies the payment, updates the order, and then 302s the
# browser to the storefront's own success/failure page (SM Settings ›
# Payment Portal › Portal Base URL). There is no Frappe-served page in
# between; SaathiMart is API-only.

@frappe.whitelist(allow_guest=True)
def esewa_success(data=None, **kwargs):
    """eSewa redirects here with ?data=<base64>. Decode, verify signature, mark order paid."""
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

    order_id = payload.get("transaction_uuid")
    if not order_id or not frappe.db.exists("Order", order_id):
        return _redirect(_frontend_redirect("/payment/failure", error="Order not found"))

    amount = flt(payload.get("total_amount", 0))
    order = frappe.get_doc("Order", order_id)
    if abs(amount - flt(order.grand_total)) > 1:
        return _redirect(_frontend_redirect(
            "/payment/failure", order_id=order_id, error="Amount mismatch",
        ))

    _mark_order_paid(
        order_id,
        gateway="eSewa",
        reference=payload.get("transaction_code", ""),
        transaction_uid=order_id,
        amount=amount,
    )
    return _redirect(_frontend_redirect("/payment/success", order_id=order_id, gateway="eSewa"))


@frappe.whitelist(allow_guest=True)
def esewa_failure(data=None, **kwargs):
    """eSewa redirects here with ?data=<base64> when the shopper cancels or the payment fails."""
    payload = _decode_esewa_data(data, kwargs)
    order_id = (payload or {}).get("transaction_uuid") or kwargs.get("order_id")
    if order_id and frappe.db.exists("Order", order_id):
        _mark_order_failed(order_id, gateway="eSewa", message="Payment cancelled or failed")
    return _redirect(_frontend_redirect(
        "/payment/failure", order_id=order_id, error="Payment cancelled or failed",
    ))


@frappe.whitelist(allow_guest=True)
def khalti_callback(pidx=None, order_id=None, **kwargs):
    """Khalti redirects here with ?pidx=...&order_id=... (order_id from our own return_url)."""
    if not pidx:
        return _redirect(_frontend_redirect(
            "/payment/failure", order_id=order_id, error="Missing pidx",
        ))

    result = _lookup_khalti(pidx)
    if not result.get("ok"):
        if order_id and frappe.db.exists("Order", order_id):
            _mark_order_failed(order_id, gateway="Khalti", message=result.get("error"))
        return _redirect(_frontend_redirect(
            "/payment/failure", order_id=order_id, error=result.get("error"),
        ))

    resolved_order = result.get("purchase_order_id") or order_id
    if not resolved_order or not frappe.db.exists("Order", resolved_order):
        return _redirect(_frontend_redirect("/payment/failure", error="Order not found"))

    if result.get("status") != "Completed":
        _mark_order_failed(resolved_order, gateway="Khalti", message=f"Status: {result.get('status')}")
        return _redirect(_frontend_redirect(
            "/payment/failure", order_id=resolved_order,
            error=f"Payment not completed: {result.get('status')}",
        ))

    _mark_order_paid(
        resolved_order,
        gateway="Khalti",
        reference=result.get("transaction_id", ""),
        transaction_uid=pidx,
        amount=flt(result.get("total_amount", 0)) / 100,
    )
    return _redirect(_frontend_redirect("/payment/success", order_id=resolved_order, gateway="Khalti"))


@frappe.whitelist(allow_guest=True)
def verify_esewa_status(order_id):
    """Poll eSewa transaction status API — used by cron to catch lost callbacks."""
    s = _settings()
    order = frappe.get_doc("Order", order_id)
    if order.payment_status == "Paid":
        return {"status": "Paid"}

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
                "total_amount": str(flt(order.grand_total)),
                "transaction_uuid": order_id,
            },
            timeout=20,
        )
        data = resp.json()
        if resp.ok and data.get("status") == "COMPLETE":
            _mark_order_paid(order_id, gateway="eSewa",
                             reference=data.get("ref_id", ""), transaction_uid=order_id,
                             amount=flt(order.grand_total))
            return {"status": "Paid", "ref_id": data.get("ref_id")}
        return {"status": data.get("status", "unknown")}
    except Exception as e:
        return {"status": "error", "error": str(e)}


# ── Internal helpers ──────────────────────────────────────────────────────────

def _decode_esewa_data(data, kwargs):
    if data:
        try:
            return json.loads(base64.b64decode(data))
        except Exception:
            return None
    # Flat params fallback
    if kwargs.get("signed_field_names"):
        return kwargs
    return None


def _lookup_khalti(pidx):
    s = _settings()
    secret_key = _get_password(s, "khalti_secret_key")
    if not secret_key:
        return {"ok": False, "error": "Khalti Secret Key not configured"}
    if secret_key.lower().startswith("key "):
        secret_key = secret_key.split(" ", 1)[1].strip()

    sandbox = bool(getattr(s, "payment_sandbox_mode", 1))
    gw_base = (
        getattr(s, "khalti_base_url", None)
        or ("https://dev.khalti.com/api/v2" if sandbox else "https://khalti.com/api/v2")
    ).rstrip("/")

    try:
        resp = requests.post(
            f"{gw_base}/epayment/lookup/",
            headers={"Authorization": f"key {secret_key}", "Content-Type": "application/json"},
            data=json.dumps({"pidx": pidx}),
            timeout=20,
        )
        data = resp.json()
        if not resp.ok:
            return {"ok": False, "error": data.get("detail", "Lookup failed")}
        return {"ok": True, **data}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _mark_order_paid(order_id, gateway, reference, transaction_uid, amount):
    """Update SM Order + create SM Payment Log. Idempotent."""
    if frappe.db.get_value("Order", order_id, "payment_status") == "Paid":
        return  # already processed

    frappe.db.set_value("Order", order_id, {
        "payment_status": "Paid",
        "payment_method": gateway,
        "payment_reference": reference,
    })

    log = frappe.new_doc("Payment Log")
    log.order = order_id
    log.gateway = gateway
    log.status = "Success"
    log.amount = amount
    log.reference = reference
    log.transaction_uid = transaction_uid
    log.insert(ignore_permissions=True)
    frappe.db.commit()

    try:
        from saathimart.api.mailing import send_order_confirmation
        doc = frappe.get_doc("Order", order_id)
        items_summary = [
            {"product_name": i.product_name, "qty": i.qty, "rate": i.rate}
            for i in doc.items
        ]
        if doc.customer_email:
            send_order_confirmation(doc.customer_email, order_id, amount, items_summary)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Payment confirmation email failed")


def _mark_order_failed(order_id, gateway, message):
    frappe.db.set_value("Order", order_id, {"payment_status": "Unpaid"})

    log = frappe.new_doc("Payment Log")
    log.order = order_id
    log.gateway = gateway
    log.status = "Failed"
    log.amount = 0
    log.notes = message or ""
    log.insert(ignore_permissions=True)

    _release_order_reservations(order_id)
    frappe.db.commit()


def _release_order_reservations(order_id):
    """Release stock reservations for all items in an order."""
    from saathimart.api.stock import release_reservation
    items = frappe.get_all(
        "Order Item",
        filters={"parent": order_id},
        fields=["product", "qty", "vendor"],
    )
    for item in items:
        if item.vendor and item.product and item.qty:
            release_reservation(item.vendor, item.product, item.qty)


def poll_pending_esewa_orders():
    """
    Cron every 10 min — check eSewa status for orders that are
    still Unpaid after 10+ minutes (lost callback recovery).
    """
    pending = frappe.get_list(
        "Order",
        filters={
            "payment_method": "eSewa",
            "payment_status": "Unpaid",
            "creation": ["<", frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-10)],
        },
        fields=["name"],
        limit=20,
    )
    for row in pending:
        try:
            verify_esewa_status(row.name)
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"eSewa poll failed for {row.name}")
