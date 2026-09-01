"""
Zero-downtime webhook secret rotation.

Rotating a shared secret between two live sites has one hard constraint:
at no instant may a legitimately-signed request be rejected. Flipping both
sides "at the same time" is impossible over a network, so instead we make
the receiver temporarily accept TWO secrets and walk through phases:

  Phase 1 (stage)    Hub -> vendor: "here is the NEXT secret". Vendor stores
                     it in webhook_secret_next and now verifies signatures
                     against {current, old, next} — but keeps SENDING with
                     its current primary.
  Phase 2 (flip)     Hub moves current -> webhook_secret_old, next -> current.
                     Hub signs with NEW. Vendor still accepts it (via next);
                     vendor's outbound OLD-signed traffic is accepted by hub
                     via webhook_secret_old.
  Phase 3 (promote)  Hub -> vendor (signed with NEW): "promote staged".
                     Vendor moves next -> current, current -> old.
  Cleanup            Both sides' *_old fields stay until the next rotation
                     overwrites them — harmless (never sent), encrypted at
                     rest, and they keep any straggler requests verifiable.

Every phase is safe to retry; if the hub dies mid-rotation, the worst case
is the vendor accepting two secrets until the rotation is re-run.
"""
import secrets

import frappe
from frappe import _
from frappe.utils import add_to_date, now_datetime
from frappe.utils.password import get_decrypted_password, set_encrypted_password

ROTATION_DUE_DAYS = 90  # alert when a vendor's secret is older than this


def _post_to_vendor(vendor_name, method, payload):
    """
    Signed direct POST to a vendor's receive endpoint, bypassing the event
    queue — rotation must complete synchronously or not at all.
    Returns True on HTTP 200.
    """
    import json
    import urllib.parse
    from datetime import datetime, timezone

    import requests

    from saathimart.api.utils import compute_hmac_signature

    site_url = frappe.db.get_value("Vendor", vendor_name, "frappe_site_url")
    if not site_url:
        frappe.throw(_("Vendor {0} has no frappe_site_url").format(vendor_name))

    secret = get_decrypted_password(
        "Vendor", vendor_name, "webhook_secret", raise_exception=False
    ) or ""
    ts = str(int(datetime.now(timezone.utc).timestamp()))
    body = json.dumps(payload)
    parsed = urllib.parse.urlparse(site_url)
    host_header = parsed.hostname
    # Same rewrite as events.publisher._deliver_event: browser-facing vendor
    # URLs (vendorN.localhost) aren't resolvable from inside the hub's
    # network — the compose service name is.
    target_url = site_url
    if host_header in ("localhost", "vendor1.localhost", "vendor2.localhost", "vendor3.localhost"):
        target_url = parsed._replace(netloc="vendors:8000").geturl()

    resp = requests.post(
        f"{target_url}/api/method/{method}",
        data=body,
        headers={
            # Host header routes to the right site behind a shared web tier.
            "Host": host_header,
            "X-Vendor-ID": "hub",
            "X-SM-Timestamp": ts,
            "X-SM-Signature": compute_hmac_signature(secret, ts, body),
            "Content-Type": "application/json",
        },
        timeout=10,
    )
    if not resp.ok:
        frappe.throw(
            _("Vendor rejected {0}: HTTP {1}: {2}").format(method, resp.status_code, resp.text[:300])
        )
    return True


@frappe.whitelist()
def rotate_vendor_secret(vendor):
    """
    Rotate a vendor pair's webhook secret with zero downtime.
    Admin-invoked (SM Admin role gated by caller); safe to re-run.
    """
    if not frappe.db.exists("Vendor", vendor):
        frappe.throw(_("Vendor {0} not found").format(vendor))

    current = get_decrypted_password(
        "Vendor", vendor, "webhook_secret", raise_exception=False
    ) or ""
    if not current:
        frappe.throw(_("Vendor {0} has no webhook_secret to rotate").format(vendor))

    new_secret = secrets.token_urlsafe(32)

    # Phase 1: vendor accepts the new secret as staged/next
    _post_to_vendor(
        vendor,
        "saathimart_vendor.api.receive.rotate_secret_stage",
        {"new_secret": new_secret},
    )

    # Phase 2: flip the hub. From this instant hub signs with NEW while the
    # vendor still accepts it via `next`, and OLD via webhook_secret_old.
    set_encrypted_password("Vendor", vendor, new_secret, "webhook_secret")
    set_encrypted_password("Vendor", vendor, current, "webhook_secret_old")
    frappe.db.commit()

    # Phase 3: vendor promotes staged -> primary
    _post_to_vendor(
        vendor,
        "saathimart_vendor.api.receive.rotate_secret_promote",
        {},
    )

    frappe.db.set_value("Vendor", vendor, "webhook_secret_rotated_at", now_datetime())
    frappe.db.commit()

    frappe.logger("rotation").info(f"rotated webhook secret for {vendor}")
    return {"ok": True, "vendor": vendor}


def check_stale_secrets():
    """
    Daily cron. Rotation itself (rotate_vendor_secret) has always been
    fully manual/on-demand — there was no cadence at all, so a secret could
    sit unrotated indefinitely with nothing ever prompting anyone to act.
    This doesn't rotate anything automatically (rotation touches a live
    vendor site and shouldn't happen unattended) — it only surfaces which
    vendors are overdue, the same way dead_letter_alert surfaces a stuck
    queue instead of silently accepting it.
    """
    vendors = frappe.get_all(
        "Vendor",
        filters={"status": "Active"},
        fields=["name", "vendor_name", "webhook_secret_rotated_at", "creation"],
    )

    cutoff = add_to_date(now_datetime(), days=-ROTATION_DUE_DAYS)
    overdue = [
        v for v in vendors
        if (v.webhook_secret_rotated_at or v.creation) < cutoff
    ]
    if not overdue:
        return

    lines = [f"{len(overdue)} vendor(s) have a webhook secret older than {ROTATION_DUE_DAYS} days:"]
    for v in overdue:
        last = v.webhook_secret_rotated_at or v.creation
        lines.append(f"  {v.vendor_name} ({v.name}): last rotated {last or 'never'}")
    body = "\n".join(lines)

    recipients = [
        r.name for r in frappe.get_all(
            "Has Role",
            filters={"role": "System Manager", "parenttype": "User"},
            pluck="parent",
            distinct=True,
        )
        if frappe.db.get_value("User", r, "enabled")
    ]
    if recipients:
        try:
            frappe.sendmail(
                recipients=recipients,
                subject=f"SaathiMart — {len(overdue)} vendor webhook secret(s) overdue for rotation",
                message="<pre>{0}</pre>".format(body),
            )
        except Exception:
            pass

    frappe.log_error(body, "Webhook Secret Rotation Overdue")
