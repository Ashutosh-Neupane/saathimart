"""App configuration for the website/Next.js client.

Centralises the small business knobs the frontend used to hardcode
(rider tip presets today; delivery SLAs, loyalty display caps, etc. can
slot in later). The FE caches this aggressively — it changes ~never, and
a change should ride out within the cache TTL, not require a deploy.
"""

import frappe

from saathimart.api.responses import handle_api_errors

# ₹ presets shown on the payment step. Order matters — rendered as-is.
DEFAULT_RIDER_TIP_OPTIONS = [0, 20, 50, 100]


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
    }
