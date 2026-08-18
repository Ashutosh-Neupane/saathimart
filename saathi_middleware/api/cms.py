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
