"""
Seeds Header/Footer navigation so the storefront shows real content
instead of its hardcoded frontend fallbacks on a fresh install.
Idempotent — safe to rerun (checks existence before inserting/overwriting
anything a site admin may have already customized).

Header is a flat category-strip (matches components/header/header-shell.tsx's
own NAV_LINKS fallback — same labels, same category slugs) rather than the
generic Home/Categories/About/Contact list this patch originally seeded,
which was actually a regression: it's non-empty, so the frontend used it
instead of the richer hardcoded default it was supposed to improve on.

Footer is 3 parent items (SaathiMart/For You/Categories), each with real
child links — components/footer.tsx renders one column per top-level item
using its children, matching DEFAULT_FOOTER_COLUMNS' 3-column shape rather
than flattening everything into a single "Navigation" column (the same
kind of regression as Header, fixed the same way: give the real data the
same shape the good default already had).

Category slugs referenced here (fruits, vegetables, dairy-and-eggs,
dairy-bakery, personal-care, cleaning-household, beverages,
ice-cream-and-desert) are seeded by seed_demo_catalog.py.
"""
import frappe

_HEADER_ITEMS = [
	# Short labels deliberately — same 9-slot strip as header-shell.tsx's own
	# NAV_LINKS fallback, whose brevity ("Dairy", "Household") is what keeps
	# it on one line; the original full names here ("Cleaning & Household",
	# "Personal Care") pushed the strip wide enough to wrap onto a second
	# line, which the single-line `flex-wrap` layout doesn't handle cleanly.
	{"label": "Daily Essentials", "url": "/categories", "sort_order": 1},
	{"label": "Fruits", "url": "/categories?categorylist=fruits", "sort_order": 2},
	{"label": "Vegetables", "url": "/categories?categorylist=vegetables", "sort_order": 3},
	{"label": "Dairy", "url": "/categories?categorylist=dairy-and-eggs", "sort_order": 4},
	{"label": "Bakery", "url": "/categories?categorylist=dairy-bakery", "sort_order": 5},
	{"label": "Beauty", "url": "/categories?categorylist=personal-care", "sort_order": 6},
	{"label": "Household", "url": "/categories?categorylist=cleaning-household", "sort_order": 7},
	{"label": "Beverages", "url": "/categories?categorylist=beverages", "sort_order": 8},
	{"label": "Desserts", "url": "/categories?categorylist=ice-cream-and-desert", "sort_order": 9},
]

# (label, url, [(child_label, child_url), ...])
_FOOTER_GROUPS = [
	("SaathiMart", [
		("About Us", "/about"),
		("Blogs", "/blogs"),
		("Careers", "/careers"),
		("Contact Us", "/contact"),
	]),
	("For You", [
		("Help and Support", "/support"),
		("Track Order", "/orders"),
		("Partner with Us", "/partner"),
		("Become a Rider", "/rider"),
	]),
	("Categories", [
		("Vegetables", "/categories?categorylist=vegetables"),
		("Fruits and Dairy", "/categories?categorylist=fruits,dairy-and-eggs"),
		("Personal Care", "/categories?categorylist=personal-care"),
		("Beverages", "/categories?categorylist=beverages"),
	]),
]


def execute():
	if not frappe.db.exists("SM Navigation Item", {"menu_location": "Header"}):
		for item in _HEADER_ITEMS:
			frappe.get_doc({
				"doctype": "SM Navigation Item",
				"menu_location": "Header",
				"is_active": 1,
				**item,
			}).insert(ignore_permissions=True)

	if not frappe.db.exists("SM Navigation Item", {"menu_location": "Footer"}):
		for group_sort, (group_label, children) in enumerate(_FOOTER_GROUPS, start=1):
			# url is mandatory on this doctype even though group headers are
			# rendered as plain column titles (not links) in footer.tsx — "#"
			# is a harmless placeholder that's never actually used.
			parent = frappe.get_doc({
				"doctype": "SM Navigation Item",
				"menu_location": "Footer",
				"label": group_label,
				"url": "#",
				"is_active": 1,
				"sort_order": group_sort,
			}).insert(ignore_permissions=True)
			for child_sort, (child_label, child_url) in enumerate(children, start=1):
				frappe.get_doc({
					"doctype": "SM Navigation Item",
					"menu_location": "Footer",
					"label": child_label,
					"url": child_url,
					"parent_item": parent.name,
					"is_active": 1,
					"sort_order": child_sort,
				}).insert(ignore_permissions=True)

	frappe.db.commit()
