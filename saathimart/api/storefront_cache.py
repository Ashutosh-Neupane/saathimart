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
    """Best-effort glob delete across redis-wrapper generations.

    Frappe 16's wrapper implements wildcard deletion as delete_keys
    ("Delete keys with wildcard *", via get_keys) and has NO
    delete_keys_pattern — newer wrappers are the reverse. Try the
    pattern-named API first, fall back to the glob delete_keys. The
    previous code called only delete_keys_pattern, so on Frappe 16 every
    pattern delete raised AttributeError and was swallowed: stale keys
    served for their whole TTL. Both calls are wrapped — a busted redis
    must never break a save; TTLs bound the staleness.
    """
    cache = frappe.cache()
    try:
        pattern_deleter = getattr(cache, "delete_keys_pattern", None)
        if pattern_deleter is not None:
            pattern_deleter(pattern)
        else:
            cache.delete_keys(pattern)
    except Exception:
        pass


def bust_product_cache(product: str):
    """Kill every hub Redis key derived from a Product.

    Product changes (price, stock, status, ratings) change what every
    product list shows even though list keys never contain the product
    name — the list cache is invalidated by version bump (see
    bump_list_version), not pattern delete.
    """
    if not product:
        return
    for pattern in (
        f"sm_product:{product}:*",
        f"sm_best_listing:{product}:*",
        f"sm_best_template:{product}:*",
        f"sm_resolve_listing:{product}:*",
    ):
        _delete_pattern(pattern)
    bump_list_version()
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


def bust_cms_cache():
    """Invalidate every CMS/content cache family the storefront reads.

    Called by the storefront content seeder and available for bulk CMS
    edits; individual doctypes also bust their own keys via doc_events.
    """
    cache = frappe.cache()
    for key in (
        "sm_site_config",
        "sm_home_content",
        "sm_banners",
        "sm_popular_cities",
        "sm_locations:all",
    ):
        try:
            cache.delete_value(key)
        except Exception:
            pass
    _delete_pattern("sm_navigation:*")
    _delete_pattern("sm_page:*")
    _delete_pattern("sm_static_page:*")


def bust_brand_cache():
    frappe.cache().delete_value("sm_brands_list")
    bump_list_version()


def bust_category_cache():
    # Category tree itself is uncached (list_categories queries fresh), but
    # every product-list key carries a category dimension.
    bump_list_version()


# ── Product-list cache version (O(1) invalidation) ──────────────────────────
# list_products caches full result pages under keys embedding a version
# number. Invalidation = one INCR on the version key; superseded entries
# age out via their own 60s TTL. A pattern delete here would instead run a
# KEYS scan over the whole keyspace on EVERY product/stock/brand change —
# O(N) work that collapses under a large catalog, which is why the list
# cache never got wired up before.

_LIST_VER_KEY = "sm_list_products:ver"


def get_list_version() -> int:
    try:
        return int(frappe.cache().get_value(_LIST_VER_KEY) or 0)
    except Exception:
        return 0


def bump_list_version():
    """O(1) invalidation for the product-list cache.

    Read-modify-write through the same prefixed get_value/set_value the
    version READ uses (a raw redis INCR may bypass the wrapper's key
    prefixing). A lost update in a concurrent bump only costs ≤60s
    staleness — the superseded entries' TTL.
    """
    try:
        frappe.cache().set_value(_LIST_VER_KEY, get_list_version() + 1)
    except Exception:
        pass


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
        # Worst case: 4 attempts × 8s POST timeout + 7s backoff ≈ 40s.
        timeout=90,
        url=url,
        secret=secret,
        tags=tags,
        remark=remark,
    )
    return True


def _post_once(url: str, secret: str, tag: str, timeout: int = 8):
    """One webhook POST carrying a single tag. Returns (verdict, detail).

    verdict: "ok"    — HTTP 2xx
             "retry" — transient: transport error, timeout, 429, 5xx
             "fail"  — permanent 4xx (wrong secret → 401, disallowed tag →
                       400, wrong route → 404). Retrying cannot fix a
                       contract mismatch; it would only burn worker time
                       and spam the error log.
    """
    import requests

    try:
        resp = requests.post(
            url,
            json={"tag": tag},
            headers={"x-revalidate-secret": secret},
            timeout=timeout,
        )
        code = resp.status_code
        if 200 <= code < 300:
            return "ok", ""
        detail = f"HTTP {code} {(resp.text or '')[:300]}"
        if code >= 500 or code == 429:
            return "retry", detail
        return "fail", detail
    except Exception as e:
        # Transport errors and timeouts are transient by nature.
        return "retry", f"{type(e).__name__}: {e}"


def _record_last_status(message: str):
    """Best-effort write of the delivery outcome to the read-only
    `revalidation_last_status` display field on Settings.

    Raw db.set_value deliberately: it fires no on_update (no cache
    invalidation churn) and, unlike doc.save(), cannot clobber a
    concurrently-edited Settings doc from a background job. Truncated to
    140 chars for a tidy form display. Never raises — status recording
    must never break delivery.
    """
    try:
        frappe.db.set_value(
            "SaathiMart Settings", "SaathiMart Settings",
            "revalidation_last_status", (message or "")[:140],
            update_modified=False,
        )
    except Exception:
        pass


def _status_prefix() -> str:
    try:
        return frappe.utils.now_datetime().strftime("%Y-%m-%d %H:%M:%S ")
    except Exception:
        return ""


def _deliver_revalidation(
    url: str, secret: str, tags, remark: str = "",
    attempts: int = 4, base_delay: float = 1.0,
):
    """Background job: deliver with bounded exponential backoff.

    Retries only transient failures (transport errors, timeouts, 429, 5xx);
    permanent 4xx rejections stop immediately. Backoff is 1s, 2s, 4s, …
    (base_delay doubled per attempt); with the default 4 attempts and the
    8s per-POST timeout the worst-case job occupancy is ~40s, which fits
    the queued job's 90s budget — revisit with a dedicated retry queue if
    storefront outages ever become routine.

    Never raises. Returns True only when some attempt got HTTP 2xx from
    the storefront route; False on permanent rejection or attempt
    exhaustion. The queue ignores the return value, but the smoke tests
    assert it and it feeds a future last-status/audit layer.
    """
    import time

    attempts = max(1, int(attempts))
    detail = ""
    delivered = 0
    for tag in tags:  # one POST per tag — the FE route accepts {"tag": t}
        ok = _deliver_one_tag(
            url, secret, tag, remark, attempts, base_delay,
        )
        if ok:
            delivered += 1
    if not tags:
        return False
    if delivered == len(tags):
        _record_last_status(
            f"{_status_prefix()}delivered {delivered}/{len(tags)} tag(s) ({remark})"
        )
        return True
    _record_last_status(
        f"{_status_prefix()}delivered {delivered}/{len(tags)} tag(s) with failures ({remark})"
    )
    return False


def _deliver_one_tag(url, secret, tag, remark, attempts, base_delay):
    """Deliver one tag with bounded exponential backoff.

    Retries only transient failures (transport errors, timeouts, 429, 5xx);
    permanent 4xx rejections stop immediately. Backoff is 1s, 2s, 4s, …
    (base_delay doubled per attempt); with the default 4 attempts and the
    8s per-POST timeout the worst-case job occupancy is ~40s per tag,
    which fits the queued job's 90s budget — revisit with a dedicated
    retry queue if storefront outages ever become routine.

    Never raises. Returns True only when some attempt got HTTP 2xx from
    the storefront route; False on permanent rejection or attempt
    exhaustion. The queue ignores the return value, but the smoke tests
    assert it and it feeds a future last-status/audit layer.
    """
    import time

    for attempt in range(1, attempts + 1):
        verdict, detail = _post_once(url, secret, tag)
        if verdict == "ok":
            return True
        if verdict == "fail":
            frappe.log_error(
                f"Next.js revalidation rejected (permanent, no retry): "
                f"{detail} — tag={tag} ({remark})",
                "Storefront Revalidation",
            )
            return False
        if attempt < attempts:
            time.sleep(base_delay * (2 ** (attempt - 1)))
    frappe.log_error(
        f"Next.js revalidation failed after {attempts} attempts: "
        f"{detail} — tag={tag} ({remark})",
        "Storefront Revalidation",
    )
    return False


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

# The FE's content layer (lib/content/cms.ts) caches every hub method call
# under `content-<method-path><query>` — the coarse `content-site` tag in
# its registry is never applied to those reads. Busting the coarse tag
# alone was a no-op: hero/nav/footer served stale content for the full
# 300s TTL. This list is the FE's actual cache-key universe; revalidateTag
# on a key nothing read is a harmless no-op, so over-covering is safe.
_CMS_API = "saathimart.api.cms"
FE_CMS_CACHE_KEYS = (
    f"content-{_CMS_API}.get_site_config",
    f"content-{_CMS_API}.get_home_content",
    f"content-{_CMS_API}.get_banners",
    f"content-{_CMS_API}.get_banners?banner_type=Hero",
    f"content-{_CMS_API}.get_banners?banner_type=Promo+Strip",
    f"content-{_CMS_API}.get_trust_badges",
    f"content-{_CMS_API}.get_product_rails",
    f"content-{_CMS_API}.get_navigation?location=Header",
    f"content-{_CMS_API}.get_navigation?location=Footer",
) + tuple(
    f"content-{_CMS_API}.get_static_page?page_type={t}"
    for t in ("about", "terms", "privacy", "cookies", "careers", "partner", "rider")
)


def cms_tags(slug: str | None = None, kind: str = "content") -> list:
    if kind == "faq":
        return ["cms-faq"]
    if kind == "offers":
        tags = ["cms-offers"]
        if slug:
            tags.append(f"cms-offer-{slug}")
        return tags
    tags = list(FE_CMS_CACHE_KEYS)
    if kind == "page" and slug:
        # dynamic (non-static) Site Page read + the registry tags kept for
        # forward-compat with the FE's declared content-page:{slug} scheme
        tags.append(f"content-{_CMS_API}.get_page?slug={slug}")
        tags.append(f"content-page:{slug}")
    tags.append("content-site")
    return tags


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
