"""
CMS API — site config, pages, navigation, banners, home content.
All guest-accessible.
"""
import json

import frappe
from frappe import _
from frappe.utils import today


@frappe.whitelist(allow_guest=True)
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
def get_site_content():
    return get_site_config()


@frappe.whitelist(allow_guest=True)
def get_content(key):
    try:
        if key == "site":
            return get_site_config()
        if key == "home":
            return _get_home_content()
        if key.startswith("page:"):
            return get_page(key[5:])
        if key in ("support", "contact"):
            return _get_website_content(key)
        return None
    except frappe.DoesNotExistError:
        # frappe.throw() queues a red-indicator message via msgprint as a
        # side effect before raising — catching the exception here doesn't
        # undo that, so callers would still see an alarming "not found"
        # toast for what's really just "no content configured yet".
        frappe.clear_messages()
        return None
    except Exception:
        frappe.clear_messages()
        frappe.log_error(frappe.get_traceback(), f"get_content failed for {key}")
        return None


def _get_website_content(content_key):
    name = frappe.db.get_value(
        "SM Website Content",
        {"content_key": content_key, "published": 1},
        "name",
    )
    if not name:
        frappe.throw(_("Content not found"), frappe.DoesNotExistError)
    doc = frappe.get_doc("SM Website Content", name)
    data = doc.as_dict()
    if data.get("content_json"):
        try:
            data["content_json"] = json.loads(data["content_json"])
        except Exception:
            data["content_json"] = {}
    return data


@frappe.whitelist(allow_guest=True)
def get_home_content():
    return _get_home_content()


def _get_home_content():
    cache_key = "sm_home_content"
    cached = frappe.cache().get_value(cache_key)
    if cached:
        return cached

    result = {
        "heroSlides": _get_published_slides("SM Hero Slide", "slide_key", "sort_order"),
        "seasonalBanners": _get_published_slides("SM Seasonal Banner", "slide_key", "sort_order"),
        "trustBadges": _get_published_trust_badges(),
        "productRails": _get_product_rail_headings(),
        "marketplace": _get_homepage_settings(),
    }

    frappe.cache().set_value(cache_key, result, expires_in_sec=300)
    return result


def _get_published_slides(doctype, key_field, order_field):
    items = frappe.get_list(
        doctype,
        filters={"published": 1},
        fields=[key_field, "title_lines", "description", "image",
                "cta_label", "cta_href", "cta_secondary_label", "cta_secondary_href"],
        order_by=f"{order_field} asc",
    )
    result = []
    for item in items:
        title_lines_raw = item.get("title_lines") or ""
        title_lines = [l.strip() for l in title_lines_raw.split("\n") if l.strip()]
        if not title_lines:
            continue
        slide = {
            "id": item.get(key_field) or "",
            "image": item.get("image") or "",
            "titleLines": title_lines,
        }
        if item.get("description"):
            slide["description"] = item["description"]
        if item.get("cta_label") and item.get("cta_href"):
            slide["cta"] = {"label": item["cta_label"], "href": item["cta_href"]}
        if item.get("cta_secondary_label") and item.get("cta_secondary_href"):
            slide["ctaSecondary"] = {
                "label": item["cta_secondary_label"],
                "href": item["cta_secondary_href"],
            }
        result.append(slide)
    return result


def _get_published_trust_badges():
    items = frappe.get_list(
        "SM Trust Badge",
        filters={"published": 1},
        fields=["icon_key", "title", "description"],
        order_by="sort_order asc",
    )
    result = []
    for item in items:
        icon = item.get("icon_key") or "delivery"
        if icon not in ("delivery", "authentic", "hours", "free-delivery"):
            continue
        result.append({
            "icon": icon,
            "title": item.get("title") or "",
            "description": item.get("description") or "",
        })
    return result


def _get_product_rail_headings():
    items = frappe.get_list(
        "SM Product Rail Heading",
        filters={"published": 1},
        fields=["rail_key", "title", "subtitle"],
        order_by="sort_order asc",
    )
    result = {}
    valid_keys = {"featured", "personal-care", "dairy-bakery", "cleaning-household"}
    for item in items:
        key = item.get("rail_key")
        if key in valid_keys:
            result[key] = {
                "title": item.get("title") or "",
                "subtitle": item.get("subtitle") or "",
            }
    for key in valid_keys:
        result.setdefault(key, {"title": "", "subtitle": ""})
    return result


def _get_homepage_settings():
    try:
        doc = frappe.get_single("SM Homepage Settings")
        title_raw = doc.get("marketplace_banner_title") or ""
        title_lines = [l.strip() for l in title_raw.split("\n") if l.strip()]
        return {
            "titleLines": title_lines or ["Your Everyday Marketplace"],
            "description": doc.get("marketplace_banner_description") or "",
            "linkLabel": doc.get("marketplace_banner_link_label") or "Shop now",
        }
    except Exception:
        return {
            "titleLines": ["Your Everyday Marketplace"],
            "description": "",
            "linkLabel": "Shop now",
        }


def _bust_site_config_cache(doc, method):
    frappe.cache().delete_key("sm_site_config")


def _bust_navigation_cache(doc, method):
    for loc in ("Header", "Footer", "Mobile", "Sidebar"):
        frappe.cache().delete_key(f"sm_navigation:{loc}")


def _bust_banner_cache(doc, method):
    frappe.cache().delete_key("sm_banners")


def _bust_page_cache(doc, method):
    frappe.cache().delete_key(f"sm_page:{doc.slug}")


def _bust_blog_cache(doc, method):
    frappe.cache().delete_key(f"sm_blog:{doc.slug}")


def _bust_home_content_cache(doc, method):
    frappe.cache().delete_key("sm_home_content")


def _bust_content_cache(doc, method):
    key = getattr(doc, "content_key", None)
    if key:
        frappe.cache().delete_key(f"sm_content:{key}")
