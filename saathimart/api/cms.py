"""
CMS API — site config, pages, navigation, banners, blog.
All guest-accessible.
"""
import json

import frappe
from frappe import _
from frappe.utils import today

from saathimart.api.responses import handle_api_errors


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def get_site_config():
    cached = frappe.cache().get_value("sm_site_config")
    if cached:
        return cached
    doc = frappe.get_single("Site Config")
    data = doc.as_dict()
    # Copyright year defaults to "now" so the footer never goes stale —
    # admins can still set an explicit value (e.g. a founding year range).
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

    name = frappe.db.get_value("Site Page", {"slug": slug, "status": "Published"}, "name")
    if not name:
        frappe.throw(_("Page not found"), frappe.DoesNotExistError)

    doc = frappe.get_doc("Site Page", name)
    data = doc.as_dict()
    # Parse sections JSON so frontend gets a proper array
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
        "Navigation Item",
        filters={"menu_location": location, "is_active": 1, "parent_item": ["is", "not set"]},
        fields=["name", "label", "url", "icon", "open_in_new_tab", "sort_order"],
        order_by="sort_order asc",
    )
    # Attach children
    for item in items:
        item["children"] = frappe.get_list(
            "Navigation Item",
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

    # Filter by validity dates
    banners = frappe.get_list(
        "Banner",
        filters=filters,
        fields=["name", "title", "banner_type", "heading", "subheading",
                "cta_label", "cta_url", "cta_secondary_label", "cta_secondary_url",
                "image", "mobile_image", "bg_color", "text_color", "sort_order"],
        order_by="sort_order asc",
    )
    # Post-filter by date validity
    result = []
    for b in banners:
        doc = frappe.get_doc("Banner", b["name"])
        if doc.valid_from and str(doc.valid_from) > today():
            continue
        if doc.valid_to and str(doc.valid_to) < today():
            continue
        # Stable, URL/React-key-friendly identifier derived from the title —
        # there's no separate slug field, and the docname is an opaque hash.
        b["id"] = frappe.scrub(b["title"]).replace("_", "-")
        result.append(b)

    if not banner_type:
        frappe.cache().set_value("sm_banners", result, expires_in_sec=300)
    return result


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def get_blog_posts(category=None, tag=None, page=1, page_size=10):
    filters = {"status": "Published"}
    if category:
        filters["category"] = category

    page = max(1, int(page))
    page_size = min(50, max(1, int(page_size)))

    posts = frappe.get_list(
        "Blog Post",
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

    name = frappe.db.get_value("Blog Post", {"slug": slug, "status": "Published"}, "name")
    if not name:
        frappe.throw(_("Blog post not found"), frappe.DoesNotExistError)

    doc = frappe.get_doc("Blog Post", name)
    data = doc.as_dict()
    frappe.cache().set_value(cache_key, data, expires_in_sec=300)
    return data


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def get_site_content():
    """Alias for get_site_config — frontend §35 expects this name."""
    return get_site_config()


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def get_content(key):
    """
    Generic content fetcher. Supported keys:
      - site → Site Config
      - home → assembled HomeContent from Hero Slide + Seasonal Banner + Trust Badge + Product Rail Heading + Homepage Settings
      - page:<slug> → Site Page by slug
      - support → Website Content by content_key
      - contact → Website Content by content_key
    Returns null (Frappe-style) for unknown/missing keys so frontend falls back to defaults.
    """
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
        return None
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"get_content failed for {key}")
        return None


def _get_website_content(content_key):
    """Fetch a Website Content row by content_key."""
    name = frappe.db.get_value(
        "Website Content",
        {"content_key": content_key, "published": 1},
        "name",
    )
    if not name:
        frappe.throw(_("Content not found"), frappe.DoesNotExistError)
    doc = frappe.get_doc("Website Content", name)
    data = doc.as_dict()
    if data.get("content_json"):
        try:
            data["content_json"] = json.loads(data["content_json"])
        except Exception:
            data["content_json"] = {}
    return data


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def get_home_content():
    """
    Public wrapper for _get_home_content — assemble HomeContent JSON from
    dedicated CMS doctypes. Missing/unpublished sections return the hardcoded
    defaults from the frontend.
    """
    return _get_home_content()


def _get_home_content():
    """
    Assemble HomeContent JSON from dedicated CMS doctypes.
    Missing/unpublished sections return hardcoded defaults (seeded on setup).
    """
    cache_key = "sm_home_content"
    cached = frappe.cache().get_value(cache_key)
    if cached:
        return cached

    result = {
        "heroSlides": _get_published_slides("Hero Slide", "slide_key", "sort_order"),
        "seasonalBanners": _get_published_slides("Seasonal Banner", "slide_key", "sort_order"),
        "trustBadges": _get_published_trust_badges(),
        "productRails": _get_product_rail_headings(),
        "marketplace": _get_homepage_settings(),
    }

    frappe.cache().set_value(cache_key, result, expires_in_sec=300)
    return result


@frappe.whitelist(allow_guest=True)
def get_home_layout():
    """Everything editor-owned that the home page needs, in one round trip.

    Combines banners, trust badges, product rails, site config, and homepage
    settings into a single payload. Reduces homepage API calls from 6-7 to 1.

    This is purely editor-owned content — no product feeds, so it's
    location-independent and the same for every visitor. The product-specific
    data (deals, bestsellers, recommended) lives in home.get_homepage_data.
    """
    from saathimart.api.responses import raw
    return {
        "banners": raw(get_banners)(),
        "trust_badges": raw(_get_published_trust_badges)(),
        "product_rails": raw(_get_product_rail_headings)(),
        "site_config": raw(get_site_config)(),
        "home_content": raw(get_home_content)(),
    }


def _get_published_slides(doctype, key_field, order_field):
    """Fetch published slides/banners ordered by sort_order, adapt to frontend shape."""
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
        "Trust Badge",
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
        "Product Rail Heading",
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
        doc = frappe.get_single("Homepage Settings")
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


# ── Cache bust hooks (called from doc_events in hooks.py) ─────────────────────

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


# ── FAQ ──────────────────────────────────────────────────────────────────────


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def get_faq_categories():
    """All active FAQ categories with their items, in sort order."""
    cache_key = "sm_faq_categories"
    cached = frappe.cache().get_value(cache_key)
    if cached is not None:
        return cached

    categories = frappe.get_list(
        "FAQ Category",
        filters={"is_active": 1},
        fields=["name", "category_name", "slug", "description", "sort_order"],
        order_by="sort_order asc",
    )

    for cat in categories:
        cat["items"] = frappe.get_list(
            "FAQ Item",
            filters={"category": cat["name"], "is_active": 1},
            fields=["name", "question", "answer", "sort_order"],
            order_by="sort_order asc",
        )

    frappe.cache().set_value(cache_key, categories, expires_in_sec=300)
    return categories


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def get_faq(category=None):
    """Flat list of FAQ items, optionally filtered by category slug."""
    cache_key = f"sm_faq:{category or 'all'}"
    cached = frappe.cache().get_value(cache_key)
    if cached is not None:
        return cached

    filters = {"is_active": 1}
    if category:
        cat_name = frappe.db.get_value(
            "FAQ Category", {"slug": category, "is_active": 1}, "name"
        )
        if not cat_name:
            return []
        filters["category"] = cat_name

    items = frappe.get_list(
        "FAQ Item",
        filters=filters,
        fields=["name", "question", "answer", "category", "sort_order"],
        order_by="sort_order asc",
    )

    frappe.cache().set_value(cache_key, items, expires_in_sec=300)
    return items


def _bust_faq_category_cache(doc, method):
    frappe.cache().delete_key("sm_faq_categories")
    frappe.cache().delete_key("sm_faq:all")


def _bust_faq_item_cache(doc, method):
    frappe.cache().delete_key("sm_faq_categories")
    frappe.cache().delete_key("sm_faq:all")


# ── Offers / Promotions ──────────────────────────────────────────────────────


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def get_offers(status=None):
    """All published offers, optionally filtered by status."""
    cache_key = f"sm_offers:{status or 'all'}"
    cached = frappe.cache().get_value(cache_key)
    if cached is not None:
        return cached

    filters = {"is_active": 1}
    if status:
        filters["status"] = status
    else:
        filters["status"] = "Published"

    offers = frappe.get_list(
        "Offer",
        filters=filters,
        fields=["name", "title", "slug", "subtitle", "image", "mobile_image",
                "valid_from", "valid_to", "sort_order", "meta_title", "meta_description"],
        order_by="sort_order asc",
    )

    frappe.cache().set_value(cache_key, offers, expires_in_sec=300)
    return offers


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def get_offer(slug):
    """Single offer by slug."""
    cache_key = f"sm_offer:{slug}"
    cached = frappe.cache().get_value(cache_key)
    if cached:
        return cached

    name = frappe.db.get_value(
        "Offer", {"slug": slug, "status": "Published", "is_active": 1}, "name"
    )
    if not name:
        frappe.throw(_("Offer not found"), frappe.DoesNotExistError)

    doc = frappe.get_doc("Offer", name)
    data = doc.as_dict()
    # Parse highlights into a list
    if data.get("highlights"):
        data["highlights"] = [h.strip() for h in data["highlights"].split("\n") if h.strip()]
    else:
        data["highlights"] = []

    frappe.cache().set_value(cache_key, data, expires_in_sec=300)
    return data


def _bust_offer_cache_on_update(doc, method):
    frappe.cache().delete_key("sm_offers:all")
    if hasattr(doc, "slug") and doc.slug:
        frappe.cache().delete_key(f"sm_offer:{doc.slug}")


# ── Popular Locations ────────────────────────────────────────────────────────


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def get_popular_locations(city=None):
    """All active popular locations, optionally filtered by city."""
    cache_key = f"sm_locations:{city or 'all'}"
    cached = frappe.cache().get_value(cache_key)
    if cached is not None:
        return cached

    filters = {"is_active": 1}
    if city:
        filters["city"] = city

    locations = frappe.get_list(
        "Popular Location",
        filters=filters,
        fields=["name", "location_name", "slug", "city", "district",
                "latitude", "longitude", "sort_order"],
        order_by="sort_order asc",
    )

    frappe.cache().set_value(cache_key, locations, expires_in_sec=300)
    return locations


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def get_popular_cities():
    """Distinct cities from popular locations."""
    cache_key = "sm_popular_cities"
    cached = frappe.cache().get_value(cache_key)
    if cached is not None:
        return cached

    cities = frappe.get_all(
        "Popular Location",
        filters={"is_active": 1},
        fields=["city"],
        distinct=True,
        order_by="city asc",
    )
    result = [c["city"] for c in cities if c.get("city")]

    frappe.cache().set_value(cache_key, result, expires_in_sec=300)
    return result


def _bust_location_cache_on_update(doc, method):
    frappe.cache().delete_key("sm_locations:all")
    frappe.cache().delete_key("sm_popular_cities")
    if hasattr(doc, "city") and doc.city:
        frappe.cache().delete_key(f"sm_locations:{doc.city}")


# ── Static Pages (dedicated Single DocTypes) ──────────────────────────────────

STATIC_PAGE_DOCTYPE_MAP = {
    "about": "About Us",
    "terms": "Terms Page",
    "privacy": "Privacy Page",
    "cookies": "Cookies Page",
    "careers": "Careers Page",
    "partner": "Partner Page",
    "rider": "Rider Page",
}


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def get_static_page(page_type):
    """Return content for a dedicated static-page Single DocType.

    ``page_type`` is one of: about, terms, privacy, cookies, careers,
    partner, rider. Each maps to its own DocType so editors get a focused
    form instead of a generic page row — mirrors the legacy storefront's
    get_static_page, but see STATIC_PAGE_DOCTYPE_MAP above for the (renamed,
    no "Saathi " prefix) doctypes this app actually uses.
    """
    if page_type not in STATIC_PAGE_DOCTYPE_MAP:
        frappe.throw(_("Invalid page type"), frappe.DoesNotExistError)

    doctype = STATIC_PAGE_DOCTYPE_MAP[page_type]
    cache_key = f"sm_static_page:{page_type}"

    cached = frappe.cache().get_value(cache_key)
    if cached:
        return cached

    doc = frappe.get_single(doctype)
    data = doc.as_dict()
    # Single DocTypes omit empty fields — ensure all expected keys exist
    for field in ("title", "breadcrumb_label", "subtitle", "meta_title",
                  "meta_description", "hero_title", "hero_subtitle",
                  "mission_title", "mission_text", "features_title",
                  "values_title", "cta_title", "cta_text"):
        if field not in data:
            data[field] = ""
    # Parse JSON fields (sections, etc.)
    for field in ("sections",):
        if data.get(field):
            try:
                data[field] = json.loads(data[field])
            except Exception:
                data[field] = []
        else:
            data[field] = []
    # Serialize child table fields into plain lists
    for field in ("stats", "features", "values"):
        if hasattr(doc, field) and doc.get(field):
            data[field] = [row.as_dict() for row in doc.get(field)]
        else:
            data[field] = []

    frappe.cache().set_value(cache_key, data, expires_in_sec=300)
    return data


def _bust_static_page_cache(doc, method):
    """Invalidate cache for whichever static page DocType was updated.

    Wired from hooks.py's doc_events, same pattern as every other CMS
    doctype in this file — the doctype controllers themselves stay bare.
    """
    for page_type, doctype in STATIC_PAGE_DOCTYPE_MAP.items():
        if doc.doctype == doctype:
            frappe.cache().delete_key(f"sm_static_page:{page_type}")
            return


# ── Contact Submissions ──────────────────────────────────────────────────────


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def submit_contact(full_name, email, message, phone=None, subject=None):
    """Submit a contact form message. Public endpoint."""

    doc = frappe.get_doc({
        "doctype": "Contact Submission",
        "full_name": full_name,
        "email": email,
        "phone": phone,
        "subject": subject,
        "message": message,
        "status": "New",
    })
    doc.insert(ignore_permissions=True)
    frappe.db.commit()
    return {"ok": True, "message": "Message sent successfully."}
