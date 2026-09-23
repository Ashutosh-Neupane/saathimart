"""App configuration for the website/Next.js client.

Centralises the small business knobs the frontend used to hardcode
(rider tip presets, per-city delivery ETAs). The FE caches this
aggressively — it changes ~never, and a change should ride out within
the cache TTL, not require a deploy.
"""

import frappe

from saathimart.api.responses import handle_api_errors

# ₹ presets shown on the payment step. Order matters — rendered as-is.
DEFAULT_RIDER_TIP_OPTIONS = [0, 20, 50, 100]

# ETA fallback when a city has no zone with eta_minutes set.
DEFAULT_ETA_MINUTES = 120


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def get_app_config():
    """Public app configuration for the website client."""
    settings = frappe.get_cached_doc("SaathiMart Settings")

    raw = getattr(settings, "rider_tip_options", None)
    tips: list[int] = []
    if raw:
        for part in str(raw).split(","):
            part = part.strip()
            if part.isdigit():
                tips.append(int(part))
    if not tips:
        tips = list(DEFAULT_RIDER_TIP_OPTIONS)

    return {
        "rider_tip_options": tips,
        "default_rider_tip": tips[1] if len(tips) > 1 else 0,
        "currency": "NPR",
        "eta_by_city": _eta_by_city(),
        "default_eta_minutes": DEFAULT_ETA_MINUTES,
    }


def _eta_by_city():
    """City → express ETA in minutes, from active Delivery Zones.

    The FE previously hardcoded a per-city ETA map (Kathmandu 10, Pokhara
    60, ...). The map now lives here, editable per zone, and rides out to
    the FE through app_config. Zones without a city match their zone_name.
    """
    rows = frappe.get_all(
        "Delivery Zone",
        filters={"is_active": 1},
        fields=["zone_name", "city", "eta_minutes"],
    )
    out: dict[str, int] = {}
    for r in rows:
        minutes = int(r.eta_minutes or 0)
        if minutes <= 0:
            continue
        key = (r.city or "").strip()
        if not key:
            # Zones without a real city are internal/test fixtures — never
            # expose their names as city keys to the frontend.
            continue
        # First (fastest) zone wins when several zones cover a city.
        if key not in out or minutes < out[key]:
            out[key] = minutes
    return out
