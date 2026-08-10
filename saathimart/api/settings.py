"""
SaathiMart Settings — centralized access to the monolithic Settings Single DocType.

All app code should import from here rather than calling
frappe.get_single("Settings") directly. This module provides:
  - Cached single-doc access
  - Typed getters with sensible defaults
  - A single place to split Settings into domain-specific docs later
"""
from __future__ import annotations

import frappe
from frappe.utils import flt


_cache_key = "sm_settings_doc"
_cache_ttl = 300


def get_settings():
    cached = frappe.cache().get_value(_cache_key)
    if cached:
        return cached
    doc = frappe.get_single("Settings")
    frappe.cache().set_value(_cache_key, doc, expires_in_sec=_cache_ttl)
    return doc


def invalidate_settings_cache():
    frappe.cache().delete_key(_cache_key)


def is_loyalty_enabled():
    s = get_settings()
    return bool(getattr(s, "enable_loyalty", 0)) and bool(getattr(s, "loyalty_program", None))


def is_coupon_enabled():
    return bool(getattr(get_settings(), "enable_coupons", 1))


def get_payment_methods():
    s = get_settings()
    raw = getattr(s, "payment_methods", "COD") or "COD"
    return [m.strip() for m in raw.split("\n") if m.strip()]


def is_gateway_enabled(gateway: str) -> bool:
    s = get_settings()
    if gateway == "eSewa":
        return bool(getattr(s, "enable_esewa", 1))
    if gateway == "Khalti":
        return bool(getattr(s, "enable_khalti", 0))
    return False


def get_gateway_credentials(gateway: str) -> dict:
    s = get_settings()
    if gateway == "eSewa":
        return {
            "merchant_code": getattr(s, "esewa_merchant_code", "") or "EPAYTEST",
            "secret_key": s.get_password("esewa_secret_key", raise_exception=False) or "",
            "base_url": (getattr(s, "esewa_base_url", None) or "").rstrip("/") or None,
        }
    if gateway == "Khalti":
        return {
            "secret_key": s.get_password("khalti_secret_key", raise_exception=False) or "",
            "base_url": (getattr(s, "khalti_base_url", None) or "").rstrip("/") or None,
        }
    return {}


def get_payment_pending_expiry_hours() -> int:
    return int(getattr(get_settings(), "payment_pending_order_expiry_hours", 24) or 24)


def get_cart_expiry_days() -> int:
    return int(getattr(get_settings(), "cart_expiry_days", 7) or 7)


def get_abandoned_cart_hours() -> int:
    return int(getattr(get_settings(), "abandoned_cart_hours", 24) or 24)


def get_min_order_amount() -> float:
    return flt(getattr(get_settings(), "min_order_amount", 0) or 0)


def get_free_delivery_above() -> float:
    return flt(getattr(get_settings(), "free_delivery_above", 0) or 0)


def is_sandbox_mode() -> bool:
    return bool(getattr(get_settings(), "payment_sandbox_mode", 1))


def get_payment_portal_base_url() -> str:
    s = get_settings()
    return (getattr(s, "payment_portal_base_url", None) or "").strip()


def get_webhook_secret() -> str:
    s = get_settings()
    return s.get_password("webhook_secret", raise_exception=False) or ""


def get_event_channel() -> str:
    return getattr(get_settings(), "event_channel", "saathimart:events") or "saathimart:events"


def get_redis_queue_url() -> str:
    return getattr(get_settings(), "redis_queue_url", "redis://redis-queue:6379") or "redis://redis-queue:6379"


def get_retain_webhook_events_days() -> int:
    return int(getattr(get_settings(), "retain_webhook_events_days", 30) or 30)


def get_retain_sync_outbox_days() -> int:
    return int(getattr(get_settings(), "retain_sync_outbox_days", 30) or 30)


def get_retain_cart_days() -> int:
    return int(getattr(get_settings(), "retain_cart_days", 90) or 90)


def get_retain_orders_days() -> int:
    return int(getattr(get_settings(), "retain_orders_days", 365) or 365)


def is_b2b_enabled() -> bool:
    return False


def get_calendar_mode() -> str:
    return "AD"


@frappe.whitelist(allow_guest=True)
def get_public_settings():
    """
    Guest-safe subset of Settings for the storefront: payment methods,
    currency, order minimums, and feature flags. Never expose gateway
    credentials or webhook secrets here.
    """
    s = get_settings()
    return {
        "currency": getattr(s, "currency", None) or "NPR",
        "payment_methods": get_payment_methods(),
        "min_order_amount": get_min_order_amount(),
        "free_delivery_above": get_free_delivery_above(),
        "is_coupon_enabled": is_coupon_enabled(),
        "is_loyalty_enabled": is_loyalty_enabled(),
    }
