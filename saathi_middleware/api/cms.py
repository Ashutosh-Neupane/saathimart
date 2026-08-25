"""
CMS API — site config, pages, navigation, banners, home content.
All guest-accessible.
"""
import json

import frappe

from saathi_middleware.api.responses import handle_api_errors, raw
from frappe import _
from frappe.utils import today


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def get_site_config():
    cached = frappe.cache().get_value("sm_site_config")
    if cached:
        return cached
    doc = frappe.get_single("SM Site Config")
    data = doc.as_dict()
    if not data.get("copyright_year"):
        data["copyright_year"] = frappe.utils.now_datetime().year
    frappe.cache().set_value("sm_site_config", data, expires_in_sec=600)
    return data


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def get_page(slug):
    cache_key = f"sm_page:{slug}"
    cached = frappe.cache().get_value(cache_key)
    if cached:
        return cached

    name = frappe.db.get_value("SM Site Page", {"slug": slug, "status": "Published"}, "name")
    if not name:
        frappe.throw(_("Page not found"), frappe.DoesNotExistError)

    doc = frappe.get_doc("SM Site Page", name)
    data = doc.as_dict()
    if data.get("sections"):
        try:
            data["sections"] = json.loads(data["sections"])
        except Exception:
            data["sections"] = []
    else:
        data["sections"] = []

    frappe.cache().set_value(cache_key, data, expires_in_sec=300)
    return data


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def get_navigation(location="Header"):
    cache_key = f"sm_navigation:{location}"
    cached = frappe.cache().get_value(cache_key)
    if cached:
        return cached

    items = frappe.get_list(
        "SM Navigation Item",
        filters={"menu_location": location, "is_active": 1, "parent_item": ["is", "not set"]},
        fields=["name", "label", "url", "icon", "open_in_new_tab", "sort_order"],
        order_by="sort_order asc",
    )
    for item in items:
        item["children"] = frappe.get_list(
            "SM Navigation Item",
            filters={"parent_item": item["name"], "is_active": 1},
            fields=["name", "label", "url", "icon", "open_in_new_tab", "sort_order"],
            order_by="sort_order asc",
        )

    frappe.cache().set_value(cache_key, items, expires_in_sec=300)
    return items


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def get_banners(banner_type=None):
    cached = frappe.cache().get_value("sm_banners")
    if cached and not banner_type:
        return cached

    filters = {"is_active": 1}
    if banner_type:
        filters["banner_type"] = banner_type

    banners = frappe.get_list(
        "SM Banner",
        filters=filters,
        fields=["name", "title", "banner_type", "heading", "subheading",
                "cta_label", "cta_url", "cta_secondary_label", "cta_secondary_url",
                "image", "mobile_image", "bg_color", "text_color", "sort_order"],
        order_by="sort_order asc",
    )
    result = []
    for b in banners:
        doc = frappe.get_doc("SM Banner", b["name"])
        if doc.valid_from and str(doc.valid_from) > today():
            continue
        if doc.valid_to and str(doc.valid_to) < today():
            continue
        b["id"] = frappe.scrub(b["title"]).replace("_", "-")
        result.append(b)

    if not banner_type:
        frappe.cache().set_value("sm_banners", result, expires_in_sec=300)
    return result


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def get_trust_badges():
    """The four-ish reassurance badges under the hero.

    `icon` is a key from a fixed set, never markup — the storefront owns the
    artwork and only accepts keys it knows. Anything else invalidates the whole
    home payload and drops the page to local defaults, so the Select on the
    doctype is the guard that keeps that from happening.
    """
    cached = frappe.cache().get_value("sm_trust_badges")
    if cached is not None:
        return cached

    badges = frappe.get_list(
        "SM Trust Badge",
        filters={"is_active": 1},
        fields=["name", "icon", "title", "description", "sort_order"],
        order_by="sort_order asc",
    )
    frappe.cache().set_value("sm_trust_badges", badges, expires_in_sec=300)
    return badges


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def get_product_rails():
    """Which category rails the home page shows, in order.

    This is the piece that used to be hardcoded in the storefront: the four
    slugs (featured / personal-care / dairy-bakery / cleaning-household) lived
    in home-view.tsx, so adding a rail or pointing one at a different category
    meant a frontend release. Now it is data.

    `category_slug` is intentionally a plain Data field rather than a Link to
    SM Site Page or a category doctype: list_categories derives slugs at read
    time via _slugify(category_name), so there is no stored slug column to link
    against. The trade-off is that a typo yields an empty rail rather than a
    validation error — get_home_layout reports which slugs resolved so an admin
    can see that from the API.

    Resolves category display names to slugs so the frontend can build working
    category URLs even when an editor typed the human-readable name instead.
    """
    cached = frappe.cache().get_value("sm_product_rails")
    if cached is not None:
        return cached

    rails = frappe.get_list(
        "SM Product Rail",
        filters={"is_active": 1},
        fields=["name", "rail_id", "title", "subtitle", "category_slug",
                "page_size", "heading_size", "sort_order"],
        order_by="sort_order asc",
    )

    # Build lookups so we can normalise category_slug values that were entered
    # as display names, already-correct slugs, or anything in between.
    all_categories = frappe.get_all(
        "Saathi Item Category",
        fields=["category_name", "slug"],
    )
    name_to_slug = {c["category_name"]: c["slug"] for c in all_categories}
    slug_to_slug = {c["slug"]: c["slug"] for c in all_categories}

    for rail in rails:
        raw_slug = rail.get("category_slug")
        if not raw_slug:
            continue
        if raw_slug in name_to_slug:
            rail["category_slug"] = name_to_slug[raw_slug]
        elif raw_slug in slug_to_slug:
            rail["category_slug"] = raw_slug
        else:
            rail["category_slug"] = frappe.scrub(raw_slug)

    frappe.cache().set_value("sm_product_rails", rails, expires_in_sec=300)
    return rails


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def get_home_layout():
    """Everything editor-owned that the home page needs, in one round trip.

    The storefront was making a request per content type and each one is a
    separate cache entry with its own 5-minute window, so the sections could
    briefly disagree after an edit. One call keeps them consistent.
    """
    return {
        # raw(): let a failure raise so this endpoint reports it once at the
        # top, instead of nesting {"ok": False} under one key of a 200.
        "hero_banners": raw(get_banners)(banner_type="Hero"),
        "promo_banners": raw(get_banners)(banner_type="Promo Strip"),
        "trust_badges": raw(get_trust_badges)(),
        "product_rails": raw(get_product_rails)(),
    }


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def get_blog_posts(category=None, tag=None, page=1, page_size=10):
    filters = {"status": "Published"}
    if category:
        filters["category"] = category

    page = max(1, int(page))
    page_size = min(50, max(1, int(page_size)))

    posts = frappe.get_list(
        "SM Blog Post",
        filters=filters,
        fields=["name", "title", "slug", "author", "cover_image",
                "excerpt", "category", "tags", "published_at"],
        limit_start=(page - 1) * page_size,
        limit_page_length=page_size,
        order_by="published_at desc",
    )

    if tag:
        posts = [p for p in posts if tag in (p.get("tags") or "").split(",")]

    return {"posts": posts, "page": page, "page_size": page_size}


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def get_blog_post(slug):
    cache_key = f"sm_blog:{slug}"
    cached = frappe.cache().get_value(cache_key)
    if cached:
        return cached

    name = frappe.db.get_value("SM Blog Post", {"slug": slug, "status": "Published"}, "name")
    if not name:
        frappe.throw(_("Blog post not found"), frappe.DoesNotExistError)

    doc = frappe.get_doc("SM Blog Post", name)
    data = doc.as_dict()
    frappe.cache().set_value(cache_key, data, expires_in_sec=300)
    return data


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def get_site_content():
    return raw(get_site_config)()


def _bust_site_config_cache(doc, method):
    frappe.cache().delete_key("sm_site_config")


def _bust_navigation_cache(doc, method):
    for loc in ("Header", "Footer", "Mobile", "Sidebar"):
        frappe.cache().delete_key(f"sm_navigation:{loc}")


def _bust_banner_cache(doc, method):
    frappe.cache().delete_key("sm_banners")


def _bust_trust_badge_cache(doc, method):
    frappe.cache().delete_key("sm_trust_badges")


def _bust_product_rail_cache(doc, method):
    frappe.cache().delete_key("sm_product_rails")


def _bust_page_cache(doc, method):
    frappe.cache().delete_key(f"sm_page:{doc.slug}")


def _bust_blog_cache(doc, method):
    frappe.cache().delete_key(f"sm_blog:{doc.slug}")
