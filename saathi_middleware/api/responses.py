"""
One error shape for every whitelisted endpoint.

Errors leave this app two different ways and that is the whole problem:

  1. Thrown — `frappe.throw(...)`. Frappe puts the text in `_server_messages`
     and returns a non-200 status. The storefront's normalizeApiError already
     handles this well.

  2. Returned — a plain dict with HTTP 200. The storefront's axios layer sees
     a success, so nothing normalizes it and the caller has to know to look
     inside the payload. Only one action currently does.

Returned errors had also drifted into four mutually incompatible shapes:

    coupon.py     {"ok": False, "message": ...}          <- "message"
    loyalty.py    {"ok": False, "error": ...}            <- "error"
    auth_full.py  {"ok": False, "error": ..., "code": 401}
    payments.py   {"status": "error", "error": str(e)}   <- and leaks the
                                                            raw exception

This module does not try to abolish returned errors — some callers genuinely
need a 200 (NextAuth's authorize() treats a rejection as a crash, not a bad
password). It makes them one shape, with a machine-readable code, and stops
raw exception text reaching a shopper.

Canonical failure:

    {"ok": False, "error": "<user-facing text>", "error_code": "<CODE>"}

`error` — always that key, never "message" or "status".
`error_code` — a stable string the storefront can branch on, matching the
    ApiErrorCode union it already uses for thrown errors, so both paths
    classify identically.
"""
from __future__ import annotations

import functools
import json
import re

import frappe
from frappe import _

# Mirrors saathimart-fe's ApiErrorCode union (lib/axios/errors.ts). Kept as
# plain strings rather than an Enum so the JSON payload needs no coercion.
VALIDATION_ERROR = "VALIDATION_ERROR"
UNAUTHORIZED = "UNAUTHORIZED"
FORBIDDEN = "FORBIDDEN"
NOT_FOUND = "NOT_FOUND"
RATE_LIMITED = "RATE_LIMITED"
SERVER_ERROR = "SERVER_ERROR"

_HTTP_STATUS = {
	VALIDATION_ERROR: 417,  # Frappe's own status for ValidationError
	UNAUTHORIZED: 401,
	FORBIDDEN: 403,
	NOT_FOUND: 404,
	RATE_LIMITED: 429,
	SERVER_ERROR: 500,
}


def error_response(message, error_code=VALIDATION_ERROR, set_status=False, **extra):
	"""Build the canonical failure payload.

	`set_status` is opt-in rather than the default because these responses are
	deliberately 200s: NextAuth's authorize() cannot distinguish "wrong
	password" from "backend down" if the call rejects, so the login path needs
	a 200 carrying a failure. Endpoints with no such constraint should pass
	set_status=True so the status code carries the same meaning as the body.

	`extra` is for fields a caller must keep returning on the failure path —
	loyalty's `discount`/`points_used` zeros, for instance, whose absence would
	break arithmetic on the other side.
	"""
	if set_status:
		frappe.local.response.http_status_code = _HTTP_STATUS.get(error_code, 400)

	return {"ok": False, "error": message, "error_code": error_code, **extra}


def success_response(data=None, **extra):
	"""Counterpart for endpoints that already return an `ok` flag.

	Endpoints returning a bare payload (a list of banners, a cart summary) do
	not need this — adding an envelope everywhere would be a breaking change
	for every existing caller and buys nothing.
	"""
	payload = {"ok": True, **extra}
	if data is not None:
		payload["data"] = data
	return payload


# Frappe raises these before the endpoint is ever entered — a decorator on the
# function cannot see them. Mapped here so the after_request hook can give them
# the same shape as everything else.
_EXC_TYPE_CODES = {
	"AuthenticationError": UNAUTHORIZED,
	"SessionExpired": UNAUTHORIZED,
	"CSRFTokenError": FORBIDDEN,
	"PermissionError": FORBIDDEN,
	"DoesNotExistError": NOT_FOUND,
	"PageDoesNotExistError": NOT_FOUND,
	"RateLimitExceededError": RATE_LIMITED,
	"ValidationError": VALIDATION_ERROR,
	"MandatoryError": VALIDATION_ERROR,
	"DuplicateEntryError": VALIDATION_ERROR,
	"LinkValidationError": VALIDATION_ERROR,
}

# Framework errors name the failing function and wrap it in markup meant for
# the desk's error dialog, e.g.
#   "<details><summary>You are not permitted…</summary>Function
#    <strong>saathi_middleware.api.notifications.list_notifications</strong>
#    is not whitelisted.</details>"
# That is a developer message: it reads as gibberish to a shopper and leaks the
# internal method path. These get replaced outright rather than cleaned up.
_REPLACEMENT_MESSAGES = {
	UNAUTHORIZED: "Please sign in to continue.",
	FORBIDDEN: "Please sign in to continue.",
}

# A misspelled or removed method resolves to a ValidationError whose text is
# pure implementation detail:
#   "Failed to get method for command saathi_middleware.api.cms.does_not_exist
#    with module 'saathi_middleware.api.cms' has no attribute 'does_not_exist'"
# It names internal module paths and tells a shopper nothing, so it is matched
# by prefix and answered as a plain 404.
_METHOD_MISSING_PREFIX = "Failed to get method"

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_markup(text):
	"""Flatten desk-oriented HTML into one plain line."""
	text = _TAG_RE.sub(" ", text or "")
	return " ".join(text.split())


def raw(fn):
	"""The undecorated function, for one endpoint composing another.

	`handle_api_errors` turns exceptions into a returned payload — correct at
	the HTTP boundary, wrong in the middle of one. get_home_layout() calling
	the decorated get_banners() would embed `{"ok": False, ...}` under
	`hero_banners` and still answer 200, hiding the failure inside a
	successful-looking response. Calling through `raw()` lets the exception
	reach the outer endpoint's own handler, which reports it once, at the top.
	"""
	return getattr(fn, "__wrapped__", fn)


def _message_from_exception(exc):
	"""Recover the user-facing text `frappe.throw` queued, not the repr.

	frappe.throw does two things: it raises, and it appends the (already
	translated) message to frappe.local.message_log for the response's error
	banner. The log entry is the text a shopper should see — str(exc) is often
	empty or a bare class name.

	The log is cleared afterwards so Frappe does not *also* serialise it into
	`_server_messages`; without that, one failure arrives twice in two
	different shapes, which is the exact inconsistency this module exists to
	remove.
	"""
	message = ""
	try:
		log = frappe.get_message_log() or []
		if log:
			entry = log[-1]
			message = entry.get("message", "") if isinstance(entry, dict) else str(entry)
	except Exception:
		message = ""

	frappe.clear_messages()
	return message or str(exc) or _("Something went wrong. Please try again.")


def handle_api_errors(fn):
	"""Normalise every failure, thrown or returned, into one payload.

	Frappe's own exceptions are caught rather than re-raised. Letting them
	through produced a *second* error format — a non-200 carrying
	`_server_messages` — alongside the dicts endpoints return themselves, so a
	client had to understand both. Now `frappe.throw("Cart is empty")` and
	`return error_response("Cart is empty")` reach the caller identically.

	The HTTP status still reflects the failure (417/401/403/404/500), so
	proxies, logs and monitoring keep working; the body is simply readable
	without knowing Frappe's envelope.

	Unexpected exceptions are logged with a traceback and replaced with one
	generic line — payments.py's verify_esewa_status used to return `str(e)`
	straight to the browser.
	"""

	@functools.wraps(fn)
	def wrapper(*args, **kwargs):
		try:
			return fn(*args, **kwargs)
		except frappe.AuthenticationError as exc:
			return error_response(_message_from_exception(exc), UNAUTHORIZED, set_status=True)
		except frappe.PermissionError as exc:
			return error_response(_message_from_exception(exc), FORBIDDEN, set_status=True)
		except frappe.DoesNotExistError as exc:
			return error_response(_message_from_exception(exc), NOT_FOUND, set_status=True)
		except frappe.ValidationError as exc:
			# Frappe raises ValidationError for frappe.throw()'s default, and
			# DuplicateEntryError/MandatoryError subclass it — all curated,
			# user-facing text, so the message is kept as written.
			return error_response(_message_from_exception(exc), VALIDATION_ERROR, set_status=True)
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"Unhandled error in {fn.__name__}")
			frappe.clear_messages()
			return error_response(
				_("Something went wrong. Please try again."),
				SERVER_ERROR,
				set_status=True,
			)

	return wrapper


# ── Framework-level errors (after_request) ────────────────────────────────────

def _extract_server_message(payload):
	"""Pull the human text out of Frappe's `_server_messages` envelope."""
	raw_messages = payload.get("_server_messages")
	if not isinstance(raw_messages, str):
		return ""
	try:
		entries = json.loads(raw_messages)
	except Exception:
		return ""
	texts = []
	for entry in entries if isinstance(entries, list) else []:
		try:
			parsed = json.loads(entry) if isinstance(entry, str) else entry
			texts.append(parsed.get("message", "") if isinstance(parsed, dict) else str(parsed))
		except Exception:
			if isinstance(entry, str):
				texts.append(entry)
	return _strip_markup(" ".join(t for t in texts if t))


def normalize_error_response(response=None, request=None, **kwargs):
	"""after_request hook — give framework errors the same shape as ours.

	`handle_api_errors` can only normalise failures raised *inside* an endpoint.
	Frappe rejects a request before that for the cases that matter most in
	practice — not whitelisted, guest hitting a login-only method, CSRF, expired
	session — so those still came back as `{exc_type, _server_messages}` with
	desk-oriented HTML inside. That was the remaining inconsistency: a client
	had to parse two formats depending on *why* a call failed.

	Scoped deliberately to this app's own /api/method/saathi_middleware.* calls.
	The desk and Frappe's built-in endpoints rely on the native error format for
	their own dialogs, and rewriting those would break the admin UI.
	"""
	if response is None or request is None:
		return
	if response.status_code < 400:
		return

	path = getattr(request, "path", "") or ""
	if not path.startswith("/api/method/saathi_middleware."):
		return

	try:
		payload = json.loads(response.get_data(as_text=True) or "{}")
	except Exception:
		return
	if not isinstance(payload, dict):
		return

	# Already ours (handle_api_errors ran) — leave it alone.
	message = payload.get("message")
	if isinstance(message, dict) and message.get("ok") is False:
		return

	exc_type = payload.get("exc_type") or ""
	error_code = _EXC_TYPE_CODES.get(exc_type)
	if not error_code:
		error_code = SERVER_ERROR if response.status_code >= 500 else VALIDATION_ERROR

	text = _REPLACEMENT_MESSAGES.get(error_code) or _extract_server_message(payload)

	if text.startswith(_METHOD_MISSING_PREFIX):
		error_code = NOT_FOUND
		text = "That endpoint does not exist."
		response.status_code = 404

	if not text:
		text = "Something went wrong. Please try again."

	response.set_data(json.dumps({
		"message": {"ok": False, "error": text, "error_code": error_code}
	}))
	response.headers["Content-Type"] = "application/json"
