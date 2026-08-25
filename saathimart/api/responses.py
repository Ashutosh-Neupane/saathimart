"""
One error shape for every endpoint, ported from saathi_middleware's
api/responses.py (adapted: saathimart endpoints signal failures by raising,
so the decorator layer is not needed here — see below).

Errors leave this app two different ways:

  1. Thrown — `frappe.throw(...)`. Frappe puts the text in `_server_messages`
     and returns a non-200 status whose body is desk-oriented
     (`exc_type`, HTML-flavoured messages).

  2. Returned — a plain dict with HTTP 200 (login's "wrong password" path).
     The storefront's axios layer sees a success, so nothing normalizes it.

The `after_request` hook (`normalize_error_response`) rewrites case 1 into the
canonical payload for this app's own /api/method/saathimart.* routes, so a
thrown error and a returned one reach the storefront identically. Case 2 is
handled at the call sites via `error_response`.

Canonical failure:

    {"ok": False, "error": "<user-facing text>", "error_code": "<CODE>"}

Scoped deliberately to this app's own routes. The desk and Frappe's built-in
endpoints rely on the native error format for their own dialogs, and
rewriting those would break the admin UI.
"""
from __future__ import annotations

import json
import re

import frappe
from frappe import _

# Machine-readable codes the storefront can branch on. Plain strings rather
# than an Enum so the JSON payload needs no coercion.
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
	"""Build the canonical failure payload for endpoints that *return* errors.

	`set_status` is opt-in because some callers need a 200 even on failure —
	NextAuth's authorize() treats a rejection as a crash rather than a bad
	password, so login must answer wrong credentials inside a 200.

	`extra` is for fields a caller must keep returning on the failure path —
	loyalty preview's `discount`/`points_used` zeros, for instance.
	"""
	if set_status:
		frappe.local.response.http_status_code = _HTTP_STATUS.get(error_code, 400)

	return {"ok": False, "error": message, "error_code": error_code, **extra}


def success_response(data=None, **extra):
	"""Counterpart for endpoints that return an `ok` flag. Bare payloads (a
	list of banners, a cart summary) do not need this."""
	payload = {"ok": True, **extra}
	if data is not None:
		payload["data"] = data
	return payload


# Frappe raises these before any endpoint code runs — not whitelisted, guest
# hitting a login-only method, CSRF, expired session. Mapped so those get the
# same shape as everything else.
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
# the desk's error dialog. That reads as gibberish to a shopper and leaks the
# internal method path — replaced outright rather than cleaned up.
_REPLACEMENT_MESSAGES = {
	UNAUTHORIZED: "Please sign in to continue.",
	FORBIDDEN: "Please sign in to continue.",
}

# A misspelled or removed method resolves to a ValidationError whose text is
# pure implementation detail ("Failed to get method for command …"). Matched
# by prefix and answered as a plain 404.
_METHOD_MISSING_PREFIX = "Failed to get method"

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_markup(text):
	"""Flatten desk-oriented HTML into one plain line."""
	text = _TAG_RE.sub(" ", text or "")
	return " ".join(text.split())


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


def raw(fn):
	"""The undecorated function, for one endpoint composing another.

	`handle_api_errors` turns exceptions into a returned payload — correct at
	the HTTP boundary, wrong in the middle of one. get_home_content() calling
	the decorated get_banners() would embed `{"ok": False, ...}` under
	`hero_banners` and still answer 200, hiding the failure inside a
	successful-looking response. Calling through `raw()` lets the exception
	reach the outer endpoint's own handler, which reports it once, at the top.
	"""
	return getattr(fn, "__wrapped__", fn)


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
	import functools

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


def normalize_error_response(response=None, request=None, **kwargs):
	"""after_request hook — give framework errors the same shape as ours."""
	if response is None or request is None:
		return
	if response.status_code < 400:
		return

	path = getattr(request, "path", "") or ""
	if not path.startswith("/api/method/saathimart."):
		return

	try:
		payload = json.loads(response.get_data(as_text=True) or "{}")
	except Exception:
		return
	if not isinstance(payload, dict):
		return

	# Already ours — leave it alone.
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
