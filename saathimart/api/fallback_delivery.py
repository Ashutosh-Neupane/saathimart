"""
Fallback delivery path — if the primary webhook POST to a vendor fails,
try alternative delivery methods before giving up.

Delivery paths (in order):
  1. Primary: HMAC-signed POST to vendor's webhook URL
  2. Secondary: Poll-based — vendor pulls from hub's event API
  3. Tertiary: Email payload to vendor admin (last resort)

This ensures that even if the vendor's webhook receiver is down (firewall,
DNS issue, reverse proxy problem), the event eventually reaches them via
a different channel.
"""
import json

import frappe
from frappe import _
from frappe.utils import now_datetime, cint


def try_primary_delivery(event_doc, vendor_config):
    """Primary path: signed POST to vendor webhook URL.

    Returns (success: bool, error: str or None).
    """
    import requests

    url = vendor_config.get("webhook_url")
    if not url:
        return False, "No webhook URL configured"

    payload = json.dumps({
        "event_type": event_doc.event_type,
        "event_id": event_doc.name,
        "payload": json.loads(event_doc.payload) if event_doc.payload else {},
        "timestamp": str(now_datetime()),
    })

    try:
        from saathimart.api.utils import sign_request
        headers = sign_request(
            vendor_config.get("webhook_secret", ""),
            payload.encode(),
            vendor_name=event_doc.target_vendor,
        )
        headers["Content-Type"] = "application/json"

        resp = requests.post(url, data=payload, headers=headers, timeout=30)
        if resp.status_code < 400:
            return True, None
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as e:
        return False, str(e)


def try_secondary_delivery(event_doc, vendor_config):
    """Secondary path: make the event available via hub's pull API.

    The vendor can poll `/api/method/saathimart.api.events.get_pending_events`
    to retrieve events they missed. This works even if the vendor can't
    receive inbound connections but CAN make outbound ones.

    Returns (success: bool, reason: str).
    """
    try:
        # Mark event as available for pull (instead of pushing)
        frappe.db.set_value("Webhook Event", event_doc.name, {
            "delivery_method": "pull",
            "status": "Queued",  # keep it in queue so vendor can pull
        })

        # Also notify vendor admin via email that events are available
        _notify_vendor_events_available(event_doc, vendor_config)

        return True, "Available for pull delivery"
    except Exception as e:
        return False, str(e)


def try_tertiary_delivery(event_doc, vendor_config):
    """Tertiary path: email event payload to vendor admin.

    Last resort — sends a structured email with the event data that
    the vendor admin can manually process.

    Returns (success: bool, reason: str).
    """
    vendor_admin_email = vendor_config.get("admin_email")
    if not vendor_admin_email:
        return False, "No admin email configured"

    payload = json.loads(event_doc.payload) if event_doc.payload else {}
    subject = f"[SaathiMart] Event: {event_doc.event_type} — {event_doc.name}"
    body = f"""
<h3>SaathiMart Event Delivery</h3>
<p><strong>Event:</strong> {event_doc.event_type}</p>
<p><strong>Event ID:</strong> {event_doc.name}</p>
<p><strong>Target Vendor:</strong> {event_doc.target_vendor}</p>
<p><strong>Time:</strong> {now_datetime()}</p>
<hr>
<p><strong>Payload:</strong></p>
<pre>{json.dumps(payload, indent=2, default=str)}</pre>
<hr>
<p><em>This event could not be delivered via webhook. Please process manually
or ensure your webhook endpoint is accessible.</em></p>
"""

    try:
        frappe.sendmail(
            recipients=[vendor_admin_email],
            subject=subject,
            message=body,
        )
        return True, f"Email sent to {vendor_admin_email}"
    except Exception as e:
        return False, str(e)


def deliver_with_fallback(event_doc, vendor_config):
    """Try all delivery paths in order until one succeeds.

    Returns (success: bool, method: str, error: str or None).
    """
    # Path 1: Primary webhook
    success, error = try_primary_delivery(event_doc, vendor_config)
    if success:
        return True, "webhook", None

    frappe.logger("fallback").warning(
        f"Primary delivery failed for {event_doc.name}: {error}"
    )

    # Path 2: Pull-based delivery
    success, reason = try_secondary_delivery(event_doc, vendor_config)
    if success:
        return True, "pull", None

    # Path 3: Email last resort
    success, reason = try_tertiary_delivery(event_doc, vendor_config)
    if success:
        return True, "email", None

    return False, "all_failed", "All delivery paths exhausted"


def _notify_events_available(event_doc, vendor_config):
    """Notify vendor that events are available for pull."""
    try:
        email = vendor_config.get("admin_email")
        if email:
            frappe.sendmail(
                recipients=[email],
                subject=f"[SaathiMart] {event_doc.event_type} awaiting pickup",
                message=f"Event {event_doc.name} is available for you to pull. "
                        f"Visit your sync dashboard to retrieve pending events.",
            )
    except Exception:
        pass
