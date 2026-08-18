"""
Seeds real CMS content (hero/promo banners, static pages, corrected site
config) matching the frontend's own versioned local fallback copy
(saathimart-fe's lib/content/defaults.ts — itself carried over from the
original pre-CMS static build). The frontend already renders this exact
copy today as a fallback when the CMS is empty; this patch makes it real,
admin-editable backend content instead, so a fresh site comes up fully
seeded rather than silently running on hardcoded frontend text.

Idempotent — checks existence before inserting/overwriting anything a
site admin may have already customized (matching
seed_navigation_and_site_config's pattern).

Banner `image` is deliberately left empty: the frontend's HeroSection/
PromoBanner already fall back to bundled local artwork keyed by slide id
(smart-shopping/dashain-offers/tihar-offers/dashain/tihar) when a slide's
image is empty — same behavior as the pure-defaults path, just with real
copy now coming from the backend. Swap in a real Attach Image once actual
marketing artwork exists.
"""
import json

import frappe

# "title" is deliberately a short slug-source, not the display heading —
# the backend derives each banner's `id` via frappe.scrub(title), and
# hero-section.tsx/seasonal-banners.tsx key their BUNDLED_IMAGES fallback
# map by that exact id ("smart-shopping"/"dashain-offers"/"tihar-offers"/
# "dashain"/"tihar"). A title like "Smart Shopping Starts Here" scrubs to
# "smart-shopping-starts-here", which doesn't match anything in that map,
# so every slide would silently collapse to the same generic fallback
# image instead of its own themed one.
_HERO_BANNERS = [
	{
		"title": "Smart Shopping",
		"heading": "Smart Shopping Starts Here.",
		"subheading": "Discover thousands of quality products at unbeatable prices. Enjoy seamless online ordering or experience the convenience of shopping at your nearest SaathiMart.",
		"cta_label": "Shop Now",
		"cta_url": "/categories",
		"cta_secondary_label": "Browse Categories",
		"cta_secondary_url": "/categories",
		"sort_order": 1,
	},
	{
		"title": "Dashain Offers",
		"heading": "Dashain is Here.",
		"subheading": "Festive essentials, gifts and groceries for the whole family — delivered fast, at honest prices.",
		"cta_label": "Shop Now",
		"cta_url": "/offers/dashain",
		"cta_secondary_label": "View Offers",
		"cta_secondary_url": "/categories",
		"sort_order": 2,
	},
	{
		"title": "Tihar Offers",
		"heading": "Tihar, Made Easy.",
		"subheading": "Lights, sweets and everything for the festival of lights. Order in minutes, celebrate for days.",
		"cta_label": "Shop Now",
		"cta_url": "/offers/tihar",
		"cta_secondary_label": "View Offers",
		"cta_secondary_url": "/categories",
		"sort_order": 3,
	},
]

_PROMO_BANNERS = [
	{
		"title": "Dashain",
		"heading": "Celebrate Dashain with\nLove and Togetherness",
		"subheading": "Everything you need for a joyful Dashain, delivered to your doorstep.",
		"cta_label": "View Offer",
		"cta_url": "/offers/dashain",
		"sort_order": 1,
	},
	{
		"title": "Tihar",
		"heading": "Make This Tihar\nExtra Special",
		"subheading": "Celebrate with festive essentials, premium treats, and exclusive offers—all delivered fresh and fast by SaathiMart.",
		"cta_label": "Start Shopping",
		"cta_url": "/offers/tihar",
		"sort_order": 2,
	},
]


def _text(t):
	return {"type": "text", "text": t}


def _link(label, href):
	return {"type": "link", "label": label, "href": href}


def _strong(t):
	return {"type": "strong", "text": t}


def _para(*segments):
	return {"kind": "paragraph", "segments": list(segments)}


def _heading(t):
	return {"kind": "heading", "text": t}


def _list(*items):
	return {"kind": "list", "items": [list(i) for i in items]}


def _cta(label, href):
	return {"kind": "cta", "label": label, "href": href}


_PAGES = {
	"about": {
		"page_type": "About",
		"breadcrumb_label": "ABOUT US",
		"title": "About SaathiMart",
		"subtitle": "Smart shopping starts here — built for the Kathmandu Valley.",
		"sections": [
			_para(_text(
				"SaathiMart started with a simple idea: getting fresh groceries and "
				"everyday essentials shouldn't mean a trip across town. We partner "
				"with local stores across the Kathmandu Valley to bring genuine, "
				"quality products to your door in minutes, not hours."
			)),
			_heading("What we stand for"),
			_list(
				[_text("10-minute delivery from stores near you")],
				[_text("100% authentic products, sourced from genuine brands")],
				[_text("Open 7 AM – 11 PM, every single day")],
				[_text("Free delivery on orders over Rs. 500")],
			),
			_heading("Built for Nepal"),
			_para(_text(
				"From daily essentials to festival specials, SaathiMart is built "
				"around how Kathmandu Valley households actually shop — quick "
				"top-ups, weekly staples, and everything in between. Whether "
				"you're ordering online or stopping by a SaathiMart store, you get "
				"the same authentic products at the same fair prices."
			)),
		],
	},
	"terms": {
		"page_type": "Legal",
		"breadcrumb_label": "TERMS & CONDITIONS",
		"title": "Terms and Conditions",
		"subtitle": "Last updated 31 July 2026",
		"sections": [
			_para(_text(
				"By placing an order on SaathiMart, you agree to the terms below. "
				"Please read them alongside our Privacy Policy."
			)),
			_heading("Orders and delivery"),
			_para(_text(
				"Delivery times shown at checkout are estimates based on stock and "
				"courier availability near you, not a guarantee. Prices and "
				"availability of products may change without notice; if an item "
				"becomes unavailable after ordering, we'll contact you before "
				"substituting or refunding it."
			)),
			_heading("Payments"),
			_para(_text(
				"Orders are charged at checkout using your selected payment "
				"method. Discounts and promotional pricing apply only to eligible "
				"items and may be limited per customer or per order."
			)),
			_heading("Returns and refunds"),
			_para(_text(
				"If an item arrives damaged, expired, or incorrect, contact us "
				"within 24 hours of delivery for a replacement or refund. Fresh "
				"produce and perishables are covered by the same policy — we want "
				"every order to be right."
			)),
			_heading("Account use"),
			_para(_text(
				"You're responsible for keeping your account credentials secure. "
				"SaathiMart may suspend accounts used for fraudulent orders or "
				"abuse of promotional offers."
			)),
		],
	},
	"privacy": {
		"page_type": "Legal",
		"breadcrumb_label": "PRIVACY POLICY",
		"title": "Privacy Policy",
		"subtitle": "Last updated 31 July 2026",
		"sections": [
			_para(_text(
				"This policy explains what information SaathiMart collects, how "
				"it's used, and the choices you have. It applies to the "
				"SaathiMart website and app."
			)),
			_heading("Information we collect"),
			_list(
				[_text("Account details you provide — name, mobile number, and delivery addresses")],
				[_text("Order history, so we can show past orders and speed up reordering")],
				[_text("Basic usage data (pages visited, search queries) to improve the product catalogue and search results")],
			),
			_heading("How we use it"),
			_para(_text(
				"We use your information to process orders, coordinate delivery, "
				"provide customer support, and — only with your consent — send "
				"order updates and occasional offers. We don't sell your personal "
				"information to third parties."
			)),
			_heading("Your choices"),
			_para(
				_text("You can update your account details or request deletion of your data at any time from your account settings, or by "),
				_link("contacting us", "/contact"),
				_text("."),
			),
		],
	},
	"cookies": {
		"page_type": "Legal",
		"breadcrumb_label": "COOKIE POLICY",
		"title": "Cookie Policy",
		"subtitle": "Last updated 31 July 2026",
		"sections": [
			_para(_text(
				"SaathiMart uses cookies and similar technologies to keep you "
				"signed in, remember your delivery location and cart, and "
				"understand how the site is used so we can improve it."
			)),
			_heading("Types of cookies we use"),
			_list(
				[_strong("Essential"), _text(" — keep you signed in and your cart/wishlist in sync; the site doesn't work properly without these")],
				[_strong("Preference"), _text(" — remember your delivery location and display settings")],
				[_strong("Analytics"), _text(" — help us understand which pages and products are most useful, so we can improve them")],
			),
			_heading("Managing cookies"),
			_para(_text(
				"Most browsers let you block or delete cookies in their "
				"settings. Blocking essential cookies may prevent sign-in and "
				"cart features from working correctly."
			)),
		],
	},
	"careers": {
		"page_type": "Custom",
		"breadcrumb_label": "CAREERS",
		"title": "Careers at SaathiMart",
		"subtitle": "Help us bring smart shopping to every household in the Kathmandu Valley.",
		"sections": [
			_para(_text(
				"We're a small, fast-moving team building the quickest way to "
				"shop for groceries and essentials in Nepal. We're always "
				"looking for people who care about doing things properly — from "
				"warehouse operations to engineering to customer support."
			)),
			_heading("Open roles"),
			_para(_text(
				"We don't have a public roles board yet — send us a note about "
				"what you'd like to do and we'll get back to you."
			)),
			_cta("Get in Touch", "/contact"),
		],
	},
	"partner": {
		"page_type": "Custom",
		"breadcrumb_label": "PARTNER WITH US",
		"title": "Partner with SaathiMart",
		"subtitle": "List your store on SaathiMart and reach shoppers across the Kathmandu Valley.",
		"sections": [
			_para(_text(
				"SaathiMart partners with local grocery and essentials stores to "
				"offer 10-minute delivery without either of us building a "
				"warehouse network from scratch. You keep running your store; "
				"we bring the orders and the riders."
			)),
			_heading("Why partner with us"),
			_list(
				[_text("Reach customers already shopping near your store")],
				[_text("No upfront fees to get listed")],
				[_text("We handle delivery — you handle what you're good at")],
			),
			_cta("Apply to Partner", "/contact"),
		],
	},
	"rider": {
		"page_type": "Custom",
		"breadcrumb_label": "BECOME A RIDER",
		"title": "Become a SaathiMart Rider",
		"subtitle": "Flexible hours, fair pay, deliveries close to home.",
		"sections": [
			_para(_text(
				"SaathiMart riders are the reason 10-minute delivery works. Ride "
				"when it suits you, pick up orders from stores near you, and get "
				"paid weekly."
			)),
			_heading("What you need"),
			_list(
				[_text("A valid driving license and your own bike or scooter")],
				[_text("A smartphone for the rider app")],
				[_text("Availability in Kathmandu, Lalitpur, or Bhaktapur")],
			),
			_cta("Apply to Ride", "/contact"),
		],
	},
}


def execute():
	for b in _HERO_BANNERS:
		if not frappe.db.exists("SM Banner", {"title": b["title"]}):
			frappe.get_doc({
				"doctype": "SM Banner",
				"banner_type": "Hero",
				"is_active": 1,
				"image": "",
				**b,
			}).insert(ignore_permissions=True)

	for b in _PROMO_BANNERS:
		if not frappe.db.exists("SM Banner", {"title": b["title"]}):
			frappe.get_doc({
				"doctype": "SM Banner",
				"banner_type": "Promo Strip",
				"is_active": 1,
				"image": "",
				**b,
			}).insert(ignore_permissions=True)

	for slug, page in _PAGES.items():
		if frappe.db.exists("SM Site Page", {"slug": slug}):
			continue
		frappe.get_doc({
			"doctype": "SM Site Page",
			"slug": slug,
			"status": "Published",
			"published_at": frappe.utils.now_datetime(),
			"title": page["title"],
			"breadcrumb_label": page["breadcrumb_label"],
			"subtitle": page["subtitle"],
			"page_type": page["page_type"],
			"sections": json.dumps(page["sections"]),
		}).insert(ignore_permissions=True)

	# Correct site config to match the frontend's own default copy exactly
	# (an earlier patch — seed_navigation_and_site_config — seeded
	# placeholder text before this canonical source was identified).
	site_config = frappe.get_single("SM Site Config")
	site_config.site_title = "SaathiMart"
	site_config.tagline = "Groceries delivered in minutes across Kathmandu Valley. Built for Nepal."
	site_config.legal_name = "Sathmart Pvt. Ltd."
	site_config.copyright_year = 2026
	site_config.newsletter_email_label = "Email Address"
	site_config.newsletter_placeholder = "you@example.com"
	site_config.newsletter_button_label = "Subscribe"
	site_config.save(ignore_permissions=True)

	frappe.db.commit()
