"""
Push Notification API — Firebase Cloud Messaging integration for mobile apps.

Endpoints:
  - register_device()     : Register/update FCM token for a user
  - unregister_device()   : Remove a device token
  - send_notification()   : Send to a single user or device
  - send_bulk()           : Send to multiple users at once
  - get_user_devices()    : List registered devices for a user

Scheduled:
  - cleanup_stale_tokens() : Daily cron — remove tokens not refreshed in 90 days
"""
import frappe
from frappe import _
from frappe.utils import now_datetime, add_days, cint


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_firebase_credentials():
    """Return the FCM server key / service account from SaathiMart Settings."""
    settings = frappe.get_single("Settings")
    return getattr(settings, "fcm_server_key", None)


def _send_fcm_message(registration_ids, payload):
    """
    Send a single FCM message to one or more device tokens.

    Args:
        registration_ids: list of FCM device tokens (max 500 per request)
        payload: dict with 'title', 'body', 'data' (optional), 'image' (optional)

    Returns:
        dict: {success: int, failure: int, results: list}
    """
    import json
    import urllib.request
    import urllib.error

    server_key = _get_firebase_credentials()
    if not server_key:
        frappe.log_error("FCM server key not configured in SaathiMart Settings", "push_notifications")
        return {"success": 0, "failure": len(registration_ids), "error": "FCM not configured"}

    if not registration_ids:
        return {"success": 0, "failure": 0, "results": []}

    # Build FCM message
    message = {
        "registration_ids": registration_ids[:500],  # FCM limit per request
        "notification": {
            "title": payload.get("title", ""),
            "body": payload.get("body", ""),
        },
    }

    if payload.get("image"):
        message["notification"]["image"] = payload["image"]

    if payload.get("data"):
        message["data"] = payload["data"]

    # FCM options
    message["priority"] = payload.get("priority", "high")
    if payload.get("collapse_key"):
        message["collapse_key"] = payload["collapse_key"]
    if payload.get("ttl_seconds"):
        message["time_to_live"] = payload["ttl_seconds"]

    # Send via HTTP
    try:
        req = urllib.request.Request(
            "https://fcm.googleapis.com/fcm/send",
            data=json.dumps(message).encode("utf-8"),
            headers={
                "Authorization": f"key={server_key}",
                "Content-Type": "application/json",
            },
        )
        resp = urllib.request.urlopen(req, timeout=10)
        result = json.loads(resp.read().decode("utf-8"))

        return {
            "success": result.get("success", 0),
            "failure": result.get("failure", 0),
            "results": result.get("results", []),
            "canonical_ids": result.get("canonical_ids", 0),
        }
    except urllib.error.HTTPError as e:
        frappe.log_error(
            f"FCM HTTP error: {e.code} — {e.read().decode()[:500]}",
            "push_notifications",
        )
        return {"success": 0, "failure": len(registration_ids), "error": str(e)}
    except Exception as e:
        frappe.log_error(f"FCM send failed: {e}", "push_notifications")
        return {"success": 0, "failure": len(registration_ids), "error": str(e)}


def _get_device_tokens(user):
    """Get all active FCM tokens for a user."""
    return frappe.db.sql_list(
        """SELECT fcm_token FROM `tabSM Notification Device`
           WHERE user = %s AND is_active = 1 AND fcm_token IS NOT NULL AND fcm_token != ''
        """,
        user,
    )


# ── Whitelisted Endpoints ─────────────────────────────────────────────────────

@frappe.whitelist()
def register_device(fcm_token, platform="android", device_name=None, app_version=None):
    """
    Register or update an FCM device token for the current user.
    If the token already exists (for another user), it's reassigned to the current user.
    """
    user = frappe.session.user
    if user == "Guest":
        frappe.throw(_("Please log in to register your device"), frappe.AuthenticationError)

    if not fcm_token or len(fcm_token) < 20:
        frappe.throw(_("Invalid FCM token"))

    # Check if token already registered (possibly for another user)
    existing = frappe.db.get_value(
        "SM Notification Device",
        {"fcm_token": fcm_token},
        ["name", "user"],
    )

    if existing:
        if existing["user"] != user:
            # Token belongs to another user — reassign
            frappe.db.set_value("SM Notification Device", existing["name"], {
                "user": user,
                "platform": platform,
                "device_name": device_name,
                "app_version": app_version,
                "is_active": 1,
                "last_seen": now_datetime(),
            })
        else:
            # Same user — just update last_seen
            frappe.db.set_value("SM Notification Device", existing["name"], {
                "last_seen": now_datetime(),
                "app_version": app_version,
                "device_name": device_name,
            })
        frappe.db.commit()
        return {"ok": True, "status": "updated"}

    # New device
    doc = frappe.get_doc({
        "doctype": "SM Notification Device",
        "user": user,
        "fcm_token": fcm_token,
        "platform": platform,
        "device_name": device_name,
        "app_version": app_version,
        "is_active": 1,
        "last_seen": now_datetime(),
    })
    doc.insert(ignore_permissions=True)
    frappe.db.commit()

    return {"ok": True, "status": "registered"}


@frappe.whitelist()
def unregister_device(fcm_token):
    """Deactivate a device token (e.g. on logout)."""
    user = frappe.session.user
    if user == "Guest":
        frappe.throw(_("Please log in"), frappe.AuthenticationError)

    name = frappe.db.get_value(
        "SM Notification Device",
        {"fcm_token": fcm_token, "user": user},
        "name",
    )
    if name:
        frappe.db.set_value("SM Notification Device", name, "is_active", 0)
        frappe.db.commit()

    return {"ok": True}


@frappe.whitelist()
def send_notification(user, title, body, data=None, image=None,
                      collapse_key=None, ttl_seconds=None):
    """
    Send a push notification to a specific user.
    Requires SM Admin role or system context.
    """
    if not frappe.has_permission("SM Notification Device", "read"):
        frappe.throw(_("Insufficient permissions"), frappe.PermissionError)

    tokens = _get_device_tokens(user)
    if not tokens:
        return {"ok": True, "delivered": 0, "reason": "no_devices"}

    payload = {
        "title": title,
        "body": body,
        "data": data or {},
        "image": image,
        "collapse_key": collapse_key,
        "ttl_seconds": ttl_seconds,
    }

    result = _send_fcm_message(tokens, payload)

    # Log the notification
    _log_notification(user, title, body, data, result)

    return {"ok": True, "delivered": result["success"], "failed": result["failure"]}


@frappe.whitelist()
def send_bulk(users, title, body, data=None, image=None, collapse_key=None):
    """
    Send a push notification to multiple users.
    users: comma-separated string or JSON list of user names.
    Requires SM Admin role.
    """
    if not frappe.has_permission("SM Notification Device", "read"):
        frappe.throw(_("Insufficient permissions"), frappe.PermissionError)

    if isinstance(users, str):
        users = [u.strip() for u in users.split(",") if u.strip()]

    if not users:
        return {"ok": True, "delivered": 0, "reason": "no_users"}

    payload = {
        "title": title,
        "body": body,
        "data": data or {},
        "image": image,
        "collapse_key": collapse_key,
    }

    # Collect all tokens grouped by user
    all_tokens = []
    token_user_map = {}
    for user in users:
        tokens = _get_device_tokens(user)
        for t in tokens:
            token_user_map[t] = user
            all_tokens.append(t)

    if not all_tokens:
        return {"ok": True, "delivered": 0, "reason": "no_devices"}

    # FCM accepts max 500 tokens per request
    total_success = 0
    total_failure = 0
    for i in range(0, len(all_tokens), 500):
        batch = all_tokens[i:i+500]
        result = _send_fcm_message(batch, payload)
        total_success += result["success"]
        total_failure += result["failure"]

    return {
        "ok": True,
        "delivered": total_success,
        "failed": total_failure,
        "total_tokens": len(all_tokens),
    }


@frappe.whitelist()
def get_user_devices(user=None):
    """List registered devices for a user (defaults to current user)."""
    if user is None:
        user = frappe.session.user
    if user == "Guest":
        frappe.throw(_("Please log in"), frappe.AuthenticationError)

    devices = frappe.db.sql(
        """SELECT name, platform, device_name, app_version, is_active, last_seen
           FROM `tabSM Notification Device`
           WHERE user = %s ORDER BY last_seen DESC
        """,
        user,
        as_dict=True,
    )
    return {"devices": devices, "total": len(devices)}


@frappe.whitelist()
def get_notifications(page=1, page_size=20):
    """Get in-app notifications for the current user (paginated)."""
    user = frappe.session.user
    if user == "Guest":
        frappe.throw(_("Please log in"), frappe.AuthenticationError)

    page = cint(page) or 1
    page_size = min(cint(page_size) or 20, 50)
    offset = (page - 1) * page_size

    total = frappe.db.count("SM Notification", {"user": user})
    notifications = frappe.db.sql(
        """SELECT name, kind, title, message, read, created_at
           FROM `tabSM Notification`
           WHERE user = %s
           ORDER BY created_at DESC
           LIMIT %s OFFSET %s
        """,
        (user, page_size, offset),
        as_dict=True,
    )

    unread_count = frappe.db.count("SM Notification", {"user": user, "read": 0})

    return {
        "notifications": notifications,
        "total": total,
        "unread_count": unread_count,
        "page": page,
        "page_size": page_size,
    }


@frappe.whitelist()
def mark_read(notification_name=None, mark_all=False):
    """Mark one or all notifications as read."""
    user = frappe.session.user
    if user == "Guest":
        frappe.throw(_("Please log in"), frappe.AuthenticationError)

    if mark_all:
        frappe.db.sql(
            "UPDATE `tabSM Notification` SET read=1 WHERE user=%s AND read=0",
            user,
        )
        frappe.db.commit()
        return {"ok": True, "marked": frappe.db.changes()}

    if notification_name:
        doc = frappe.get_doc("SM Notification", notification_name)
        if doc.user != user:
            frappe.throw(_("Not your notification"), frappe.PermissionError)
        doc.read = 1
        doc.save(ignore_permissions=True)
        return {"ok": True}


# ── Internal helper for order status notifications ─────────────────────────────

def send_order_notification(user, order_name, status, message=None):
    """
    Convenience: send an order-status push notification.
    Called from order_events.py when order status changes.
    """
    status_messages = {
        "Confirmed": "Your order has been confirmed!",
        "Preparing": "Your order is being prepared.",
        "Out for Delivery": "Your order is on the way! 🛵",
        "Delivered": "Your order has been delivered. Enjoy! 🎉",
        "Cancelled": "Your order has been cancelled.",
        "Payment Received": "Payment confirmed for your order.",
    }

    title = f"Order {order_name}"
    body = message or status_messages.get(status, f"Order status: {status}")

    payload = {
        "title": title,
        "body": body,
        "data": {"order_name": order_name, "status": status},
        "collapse_key": f"order_{order_name}",
    }

    tokens = _get_device_tokens(user)
    if tokens:
        result = _send_fcm_message(tokens, payload)
        _log_notification(user, title, body, {"order_name": order_name, "status": status}, result)
        return result

    return {"success": 0, "failure": 0}


def _log_notification(user, title, body, data, fcm_result=None):
    """Log notification to SM Notification doctype."""
    try:
        doc = frappe.get_doc({
            "doctype": "SM Notification",
            "user": user,
            "kind": "system",
            "title": title,
            "message": body,
            "read": 0,
            "created_at": now_datetime(),
        })
        doc.insert(ignore_permissions=True)
    except Exception:
        frappe.log_error("Failed to log notification", "push_notifications")


# ── Scheduled Tasks ────────────────────────────────────────────────────────────

def cleanup_stale_tokens():
    """Daily cron: deactivate device tokens not seen in 90 days."""
    cutoff = add_days(now_datetime(), -90)
    frappe.db.sql(
        """UPDATE `tabSM Notification Device`
           SET is_active = 0
           WHERE is_active = 1 AND last_seen < %s
        """,
        cutoff,
    )
    frappe.db.commit()
    frappe.logger().info("Push notification: cleaned up stale device tokens")
