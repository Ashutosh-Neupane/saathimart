"""Storefront cache invalidation & Next.js revalidation webhooks.

Two responsibilities, one module:

1. Hub Redis busting. Product/Listing/Stock/Review writes must not leave
   `sm_product:*` / `sm_best_listing:*` / `sm_stock:*` keys serving stale
   reads. `invalidate_product` / `invalidate_stock` in api/cache.py existed
   but were never wired to any hook — this module is the wiring.

2. Next.js tag revalidation. The Next.js storefront caches catalog/CMS reads
   under tags (`catalog-list`, `cms-faq`, `content-*`, …) and exposes a
   secret-header-protected `/api/revalidate` route. When hub data the
   storefront displays changes, we POST the affected tags there. Tags are
   the Next.js tag universe (`catalog-list`, `cms-faq`), NOT Frappe event
   names (`product.new`).

Contract with the Next.js route (FE app/api/revalidate/route.ts):
  POST  {nextjs_base_url}/api/revalidate
  headers: x-revalidate-secret: <REVALIDATION_SECRET>
  body: {"tags": ["catalog-list", ...]}      # `tag` (single) also accepted
  200: {"revalidated": N, ...}               # 400 on disallowed/unknown tag
"""

from __future__ import annotations

import frappe
from frappe import _

# ── Settings access ─────────────────────────────────────────────────────────

_SETTINGS_CACHE_KEY = "sm_revalidation_settings"


def _reval_settings():
    """Cached read of just the revalidation fields from Settings.

    Uses frappe.cache() directly (not api.settings.get_settings) so this
    module stays dependency-free; cleared on Settings.on_update via
    clear_settings_cache below (wired in the Settings controller).
    """
    try:
        cached = frappe.cache().get_value(_SETTINGS_CACHE_KEY)
        if cached is not None:
            return cached
        row = frappe.db.get_value(
            "SaathiMart Settings", "SaathiMart Settings",
            ["enable_nextjs_revalidation", "nextjs_base_url"],
            as_dict=True,
        )
        secret = ""
        if row is not None:
            try:
                secret = frappe.utils.password.get_decrypted_password(
                    "SaathiMart Settings", "SaathiMart Settings",
                    "revalidation_secret", raise_exception=False,
                ) or ""
            except Exception:
                secret = ""
        payload = {
            "enabled": bool(row and row.enable_nextjs_revalidation),
            "base_url": (row.nextjs_base_url if row else "") or "",
            "secret": secret,
        }
        frappe.cache().set_value(_SETTINGS_CACHE_KEY, payload, expires_in_sec=300)
        return payload
    except Exception:
        # Settings table not migrated yet / fresh site — treat as disabled.
        return {"enabled": False, "base_url": "", "secret": ""}


def clear_settings_cache():
    frappe.cache().delete_value(_SETTINGS_CACHE_KEY)


def _enabled() -> bool:
    return bool(_reval_settings()["enabled"])


def _base_url() -> str:
    return (_reval_settings()["base_url"] or "").strip().rstrip("/")


def _secret() -> str:
    return _reval_settings()["secret"] or ""


# ── Hub Redis invalidation ──────────────────────────────────────────────────

def _delete_pattern(pattern: str):
    """Best-effort glob delete; falls back silently on old redis wrappers."""
    cache = frappe.cache()
    try:
        cache.delete_keys_pattern(pattern)
    except Exception:
        # Very old wrappers lack delete_keys_pattern — the short TTLs make
        # skipping pattern deletes acceptable rather than crashing a save.
        pass


def bust_product_cache(product: str):
    """Kill every hub Redis key derived from a Product.

    Includes the list cache (`sm_list_products:*`): product changes (price,
    stock, status, ratings) change what every product list shows even though
    the list key never contains the product name — its dimensions are
    category/vendor/search/sort, so the whole prefix must go.
    """
    if not product:
        return
    for pattern in (
        f"sm_product:{product}:*",
        f"sm_best_listing:{product}:*",
        f"sm_best_template:{product}:*",
        f"sm_resolve_listing:{product}:*",
        "sm_list_products:*",
    ):
        _delete_pattern(pattern)
    frappe.cache().delete_value("sm_brands_list")
    frappe.cache().delete_value("sm_listing_version")


def bust_stock_cache(vendor: str | None = None, product: str | None = None):
    """Kill stock-derived hub Redis keys (complements api.cache.invalidate_stock)."""
    cache = frappe.cache()
    if vendor and product:
        _delete_pattern(f"sm_stock:{vendor}:{product}:*")
        cache.delete_value(f"sm_stock:{vendor}:{product}")
    if vendor:
        _delete_pattern(f"sm_stock_batch:{vendor}:*")
        cache.delete_value(f"sm_stock_batch:{vendor}")
    if product:
        bust_product_cache(product)


def bust_totals_cache():
    """Kill cached cart-total computations (they embed coupon/loyalty math)."""
    _delete_pattern("sm_totals:*")


def bust_brand_cache():
    frappe.cache().delete_value("sm_brands_list")


def bust_category_cache():
    # Category tree itself is uncached (list_categories queries fresh), but
    # every product-list key carries a category dimension.
    _delete_pattern("sm_list_products:*")


# ── Next.js tag revalidation ────────────────────────────────────────────────

def _dedupe(tags):
    seen, out = set(), []
    for t in tags:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def notify_nextjs(tags, remark: str = ""):
    """POST the given Next.js tags to the storefront's revalidation route.

    Queued on `short` (never blocks a save); silent no-op when disabled or
    not configured — a missing storefront must never break a Frappe write.
    Failures land in Error Log only.
    """
    tags = _dedupe(tags or [])
    if not tags:
        return False
    if not _enabled():
        return False
    base, secret = _base_url(), _secret()
    if not base or not secret:
        frappe.log_error(
            "Next.js revalidation enabled but nextjs_base_url or "
            "revalidation_secret is missing in SaathiMart Settings",
            "Storefront Revalidation Config",
        )
        return False

    url = f"{base}/api/revalidate"
    from saathimart.api.utils import safe_enqueue

    safe_enqueue(
        _deliver_revalidation,
        queue="short",
        timeout=60,
        url=url,
        secret=secret,
        tags=tags,
        remark=remark,
    )
    return True


def _deliver_revalidation(url: str, secret: str, tags, remark: str = ""):
    """Background job: one webhook POST. Logs, never raises."""
    import requests

    try:
        resp = requests.post(
            url,
            json={"tags": list(tags)},
            headers={"x-revalidate-secret": secret},
            timeout=8,
        )
        ok = resp.status_code == 200
        if not ok:
            frappe.log_error(
                f"Next.js revalidation failed: HTTP {resp.status_code} "
                f"{(resp.text or '')[:300]} — tags={tags} ({remark})",
                "Storefront Revalidation",
            )
    except Exception as e:
        frappe.log_error(
            f"Next.js revalidation request failed: {e} — tags={tags} ({remark})",
            "Storefront Revalidation",
        )
    return True


# ── Tag mapping: hub doctypes → Next.js tags ───────────────────────────────
# Mirrors the FE tag registry (lib/cache-tags.ts + lib/content/cms.ts):
#   catalog-list          product/listing/filter/search reads
#   catalog-product-{slug} product detail reads
#   cms-faq / cms-offers / cms-offer-{slug}   FAQ + offers reads
#   content-site          all CMS content reads (site config, banners, nav,
#                         rails, badges, pages, static pages)
#   orders-list-{user} / orders-detail-{id}    order reads

def product_tags(product: str | None = None) -> list:
    tags = ["catalog-list"]
    if product:
        slug = frappe.db.get_value("Product", product, "slug") or product
        tags.append(f"catalog-product-{slug}")
    return tags


CMS_CONTENT_TAGS = ("content-site",)


def cms_tags(slug: str | None = None, kind: str = "content") -> list:
    if kind == "faq":
        return ["cms-faq"]
    if kind == "offers":
        tags = ["cms-offers"]
        if slug:
            tags.append(f"cms-offer-{slug}")
        return tags
    if kind == "page" and slug:
        return [f"content-page:{slug}", "content-site"]
    return list(CMS_CONTENT_TAGS)


# ── Hook handlers (wired in hooks.py doc_events) ────────────────────────────

def on_product_changed(doc, method=None):
    """Product insert/update/trash: hub redis + storefront tags."""
    try:
        bust_product_cache(doc.name)
        notify_nextjs(product_tags(doc.name), remark=f"Product {doc.name} {method}")
    except Exception:
        frappe.log_error(frappe.get_traceback(), "on_product_changed")


def on_vendor_listing_changed(doc, method=None):
    """Listing changes move price/availability the storefront displays."""
    try:
        bust_stock_cache(vendor=doc.vendor, product=doc.product)
        bust_product_cache(doc.product)
        notify_nextjs(product_tags(doc.product), remark=f"Listing {doc.name} {method}")
    except Exception:
        frappe.log_error(frappe.get_traceback(), "on_vendor_listing_changed")


def on_vendor_stock_changed(doc, method=None):
    try:
        bust_stock_cache(vendor=doc.vendor, product=doc.product)
        notify_nextjs(product_tags(doc.product), remark=f"Stock {doc.name} {method}")
    except Exception:
        frappe.log_error(frappe.get_traceback(), "on_vendor_stock_changed")


def on_review_changed(doc, method=None):
    try:
        bust_product_cache(doc.product)
        notify_nextjs(product_tags(doc.product), remark=f"Review {doc.name}")
    except Exception:
        frappe.log_error(frappe.get_traceback(), "on_review_changed")


def on_cms_changed(doc, method=None):
    """Any CMS doctype the storefront reads (banner, nav, rails, pages…)."""
    try:
        notify_nextjs(cms_tags(), remark=f"{doc.doctype} {doc.name} {method}")
    except Exception:
        frappe.log_error(frappe.get_traceback(), "on_cms_changed")


def on_faq_changed(doc, method=None):
    try:
        notify_nextjs(cms_tags(kind="faq"), remark=f"FAQ {doc.name} {method}")
    except Exception:
        frappe.log_error(frappe.get_traceback(), "on_faq_changed")


def on_offer_changed(doc, method=None):
    try:
        slug = getattr(doc, "slug", None)
        notify_nextjs(cms_tags(slug=slug, kind="offers"), remark=f"Offer {doc.name} {method}")
    except Exception:
        frappe.log_error(frappe.get_traceback(), "on_offer_changed")


def on_site_page_changed(doc, method=None):
    try:
        notify_nextjs(
            cms_tags(slug=getattr(doc, "slug", None), kind="page"),
            remark=f"Site Page {doc.name} {method}",
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "on_site_page_changed")


def on_order_changed(doc, method=None):
    """Order lifecycle events (created/paid/status). Per-user tag + detail."""
    try:
        tags = [f"orders-detail-{doc.name}"]
        user = getattr(doc, "owner", None) or getattr(doc, "customer_email", None)
        if user:
            tags.append(f"orders-list-{user}")
        notify_nextjs(tags, remark=f"Order {doc.name} {method}")
    except Exception:
        frappe.log_error(frappe.get_traceback(), "on_order_changed")


def on_stock_ledger_entry(doc, method=None):
    """SLE controller bumps Product.stock_qty via raw db.set_value (no doc
    events fire) — this hook is the only invalidation for those writes."""
    try:
        bust_stock_cache(vendor=doc.vendor, product=doc.product)
        bust_product_cache(doc.product)
        notify_nextjs(product_tags(doc.product), remark=f"SLE {doc.name}")
    except Exception:
        frappe.log_error(frappe.get_traceback(), "on_stock_ledger_entry")


def on_coupon_changed(doc, method=None):
    """Coupon edits change cached cart totals for up to 120s otherwise —
    the totals cache embeds discount math keyed by coupon_code."""
    try:
        bust_totals_cache()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "on_coupon_changed")


def on_brand_changed(doc, method=None):
    try:
        bust_brand_cache()
        notify_nextjs(["catalog-list"], remark=f"Brand {doc.name} {method}")
    except Exception:
        frappe.log_error(frappe.get_traceback(), "on_brand_changed")


def on_category_changed(doc, method=None):
    try:
        bust_category_cache()
        notify_nextjs(["catalog-list"], remark=f"Category {doc.name} {method}")
    except Exception:
        frappe.log_error(frappe.get_traceback(), "on_category_changed")


def on_review_rating_update(doc, method=None):
    """Chain from reviews._update_product_rating's caller — see hooks wiring."""
    on_review_changed(doc, method)
