"""
Seeds SM Trust Badge and SM Product Rail with exactly the values the
storefront currently hardcodes, so turning these into CMS content is a
no-op visually on the day it ships.

Trust badge copy mirrors saathimart-fe's lib/content/defaults.ts
`trustBadges`, and the `icon` values are the four keys in that file's
TRUST_BADGE_ICONS union — the storefront rejects any other key and drops the
whole home payload to defaults, so these strings are load-bearing.

Rail rows mirror the four listProducts() calls hardcoded in home-view.tsx
plus their headings from defaults.ts's `productRails`. Note two of those
slugs ("featured", "dairy-bakery") match no category on a stock catalog —
they are seeded anyway to preserve current behaviour exactly. Point them at
real slugs, or deactivate them, from the desk rather than in code; that is
the entire reason this doctype exists.

Idempotent — skips anything an admin has already created.
"""
import frappe

_BADGES = [
	{"icon": "delivery", "title": "10 - minute Delivery", "description": "From stores near you", "sort_order": 1},
	{"icon": "authentic", "title": "100% Authentic", "description": "Only Genuine Brands", "sort_order": 2},
	{"icon": "hours", "title": "Open 7 AM - 11 PM", "description": "Every single day", "sort_order": 3},
	{"icon": "free-delivery", "title": "Free Over Rs. 500", "description": "On every delivery", "sort_order": 4},
]

_RAILS = [
	{
		"rail_id": "featured",
		"title": "Featured this Week",
		"subtitle": "Handpicked favorites, fresh arrivals, and top picks chosen just for this week.",
		"category_slug": "featured",
		"page_size": 5,
		"heading_size": "lg",
		"sort_order": 1,
	},
	{
		"rail_id": "personal-care",
		"title": "Personal Care",
		"subtitle": "A complete range of personal care essentials for your daily grooming and hygiene routine.",
		"category_slug": "personal-care",
		"page_size": 5,
		"heading_size": "md",
		"sort_order": 2,
	},
	{
		"rail_id": "dairy-bakery",
		"title": "Dairy and Bakery",
		"subtitle": "Fresh milk and soft bakery bread delivered daily for your healthy breakfasts and everyday essentials.",
		"category_slug": "dairy-bakery",
		"page_size": 3,
		"heading_size": "md",
		"sort_order": 3,
	},
	{
		"rail_id": "cleaning-household",
		"title": "Cleaning and Household",
		"subtitle": "High-quality cleaning and home care essentials designed to keep your space fresh and comfortable.",
		"category_slug": "cleaning-household",
		"page_size": 5,
		"heading_size": "md",
		"sort_order": 4,
	},
]


def execute():
	if not frappe.db.exists("SM Trust Badge", {"is_active": 1}):
		for badge in _BADGES:
			frappe.get_doc({"doctype": "SM Trust Badge", "is_active": 1, **badge}).insert(
				ignore_permissions=True
			)

	for rail in _RAILS:
		if not frappe.db.exists("SM Product Rail", rail["rail_id"]):
			frappe.get_doc({"doctype": "SM Product Rail", "is_active": 1, **rail}).insert(
				ignore_permissions=True
			)

	frappe.db.commit()
