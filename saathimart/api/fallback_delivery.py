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
from datetime import datetime, timezone

import frappe
from frappe import _
from frappe.utils import now_datetime, cint


def try_primary_delivery(event_doc, vendor_config):
    """Primary path: signed POST to vendor webhook URL.

    Returns (success: bool, error: str or None).
    """
    from saathimart.api.connection_pool import pooled_request
    from saathimart.api.utils import compute_hmac_signature

    url = vendor_config.get("frappe_site_url")
    if not url:
        return False, "No frappe_site_url configured"

    body = json.dumps({
        "event": event_doc.event_type,
        "payload": json.loads(event_doc.payload) if event_doc.payload else {},
    })

    try:
        ts = str(int(datetime.now(timezone.utc).timestamp()))
        headers = {
            "X-SM-Timestamp": ts,
            "X-SM-Signature": compute_hmac_signature(vendor_config.get("webhook_secret", ""), ts, body),
            "Content-Type": "application/json",
        }

        status, text, error = pooled_request(
            "POST",
            f"{url}/api/method/saathimart_vendor.api.receive.receive_from_hub",
            headers=headers,
            body=body,
            timeout=30,
        )
        if error:
            return False, error
        if status < 400:
            return True, None
        return False, f"HTTP {status}: {text[:200]}"
    except Exception as e:
        return False, str(e)


def try_secondary_delivery(event_doc, vendor_config):
    """Secondary path: make the event available via hub's pull API.

    The vendor can poll `/api/method/saathimart.api.events.poll` to retrieve
    events they missed (ordered by event_seq — see events.poll). This works
    even if the vendor can't receive inbound connections but CAN make
    outbound ones.

    Returns (success: bool, reason: str).
    """
    try:
        # Mark event as available for pull (instead of pushing)
        frappe.db.set_value("Webhook Event", event_doc.name, {
            "delivery_method": "Pull",
            "status": "Queued",  # keep it in queue so vendor can pull
        })

        # Also notify vendor admin via email that events are available
        _notify_events_available(event_doc, vendor_config)

        return True, "Available for pull delivery"
    except Exception as e:
        return False, str(e)


def try_tertiary_delivery(event_doc, vendor_config):
    """Tertiary path: email event payload to vendor admin.

    Last resort — sends a structured email with the event data that
    the vendor admin can manually process.

    Returns (success: bool, reason: str).
    """
    vendor_admin_email = vendor_config.get("contact_email")
    if not vendor_admin_email:
        return False, "No contact email configured"

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
        frappe.db.set_value("Webhook Event", event_doc.name, "delivery_method", "Email")
        return True, "email", None

    return False, "all_failed", "All delivery paths exhausted"


def _notify_events_available(event_doc, vendor_config):
    """Notify vendor that events are available for pull."""
    try:
        email = vendor_config.get("contact_email")
        if email:
            frappe.sendmail(
                recipients=[email],
                subject=f"[SaathiMart] {event_doc.event_type} awaiting pickup",
                message=f"Event {event_doc.name} is available for you to pull. "
                        f"Visit your sync dashboard to retrieve pending events.",
            )
    except Exception:
        pass
