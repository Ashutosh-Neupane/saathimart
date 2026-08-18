"""
Seeds a demo franchise, the full set of Saathi Item Categories the
frontend actually references by slug, and a real product catalog spread
across them — without this, the homepage's category-based product rails
(featured/personal-care/dairy-bakery/cleaning-household — see
lib/content/defaults.ts's productRails and home-view.tsx, which filters
list_products by these exact category slugs), the header's category strip,
the footer's Categories column, and the sidebar's category filter (see
seed_navigation_and_site_config.py) all resolve to real data instead of
sitting empty. Idempotent — safe to rerun.

Category naming is deliberately exact: category_name must slugify (see
api/catalog.py's _slugify — lowercase, strip all punctuation, collapse to
hyphens) to precisely the slug each frontend call site expects. E.g.
"Dairy Bakery" -> "dairy-bakery", not "Dairy & Bakery" -> "dairy-bakery"
worked out because of luck, but "Dairy and Eggs" -> "dairy-and-eggs" would
NOT match "dairy-eggs" if someone renamed it — the exact wording here is
load-bearing, not cosmetic.
"""
import frappe

_FRANCHISE = {
	"site_code": "SM-DEMO",
	"franchise_name": "SaathiMart Kathmandu Central",
	"status": "Active",
	"city": "Kathmandu",
	"latitude": 27.7172,
	"longitude": 85.3240,
	# Only one real franchise exists in this seed, and the storefront's own
	# copy already claims nationwide coverage ("Delivering all over Nepal",
	# lib/location/delivery-zone.ts) — a Kathmandu-only 12km radius would
	# make every location outside the valley fall through as "not
	# serviceable" (list_products/get_nearby_items distance filter,
	# checkout()'s _get_serviceable_franchise) even though the frontend
	# never told the shopper to expect that. 800km comfortably spans
	# Nepal east-west from a Kathmandu center point. Revisit once there
	# are real franchises outside the valley to actually zone against.
	"serviceable_radius_km": 800,
	"delivery_base_charge": 30,
	"free_delivery_upto_km": 2,
	"delivery_per_km_rate": 10,
}

# category_name -> image (always blank; see catalog.py's list_categories —
# frontend falls back to a placeholder when empty, same pattern as banners)
_CATEGORIES = [
	"Featured",
	"Personal Care",
	"Dairy Bakery",
	"Cleaning Household",
	"Fruits",
	"Vegetables",
	"Dairy And Eggs",
	"Ice Cream And Desert",
	"Beverages",
]

# (item_code, item_name, category, price_npr, stock_qty, description)
_ITEMS = [
	# Featured — flagship/popular items, distinct from the category-specific rows
	("FEAT-001", "Basmati Rice 5kg", "Featured", 850, 40, "Premium long-grain basmati rice."),
	("FEAT-002", "Extra Virgin Olive Oil 500ml", "Featured", 950, 25, "Cold-pressed extra virgin olive oil."),
	("FEAT-003", "Organic Honey 250g", "Featured", 480, 30, "Raw, unprocessed Himalayan honey."),
	("FEAT-004", "Almonds 500g", "Featured", 720, 35, "Premium California almonds."),
	("FEAT-005", "Green Tea Bags (25 pcs)", "Featured", 220, 60, "Antioxidant-rich green tea bags."),

	# Personal Care
	("PC-001", "Herbal Shampoo 200ml", "Personal Care", 260, 50, "Sulfate-free herbal shampoo."),
	("PC-002", "Handwash Liquid 500ml", "Personal Care", 180, 70, "Antibacterial handwash."),
	("PC-003", "Toothpaste 150g", "Personal Care", 145, 80, "Fluoride toothpaste for cavity protection."),
	("PC-004", "Body Lotion 200ml", "Personal Care", 320, 45, "Moisturizing body lotion for daily use."),
	("PC-005", "Face Wash 100ml", "Personal Care", 210, 55, "Gentle foaming face wash."),

	# Dairy Bakery
	("DB-001", "Fresh Milk 1L", "Dairy Bakery", 95, 100, "Pasteurized full-cream milk."),
	("DB-002", "Brown Bread 400g", "Dairy Bakery", 85, 60, "Whole wheat brown bread, baked fresh daily."),
	("DB-003", "Paneer 200g", "Dairy Bakery", 150, 40, "Fresh cottage cheese."),
	("DB-004", "Butter 100g", "Dairy Bakery", 130, 50, "Creamy salted butter."),
	("DB-005", "Curd 400g", "Dairy Bakery", 70, 65, "Fresh homemade-style curd."),

	# Cleaning Household
	("CH-001", "Dishwash Liquid 500ml", "Cleaning Household", 160, 55, "Grease-cutting dishwash liquid."),
	("CH-002", "Floor Cleaner 1L", "Cleaning Household", 210, 40, "Disinfectant floor cleaner."),
	("CH-003", "Laundry Detergent 1kg", "Cleaning Household", 290, 45, "Stain-removing laundry powder."),
	("CH-004", "Toilet Cleaner 500ml", "Cleaning Household", 175, 50, "Deep-clean toilet cleaner."),
	("CH-005", "Air Freshener 300ml", "Cleaning Household", 195, 35, "Long-lasting room air freshener."),

	# Fruits
	("FR-001", "Bananas (dozen)", "Fruits", 120, 80, "Fresh ripe bananas."),
	("FR-002", "Apples 1kg", "Fruits", 280, 60, "Crisp red apples."),
	("FR-003", "Oranges 1kg", "Fruits", 190, 55, "Juicy seasonal oranges."),

	# Vegetables
	("VEG-001", "Tomatoes 1kg", "Vegetables", 80, 90, "Fresh red tomatoes."),
	("VEG-002", "Potatoes 1kg", "Vegetables", 60, 100, "Farm-fresh potatoes."),
	("VEG-003", "Onions 1kg", "Vegetables", 75, 95, "Fresh red onions."),

	# Dairy And Eggs
	("DE-001", "Eggs Tray (30 pcs)", "Dairy And Eggs", 420, 40, "Farm-fresh eggs, tray of 30."),
	("DE-002", "Cheese Slices 200g", "Dairy And Eggs", 260, 35, "Processed cheese slices."),

	# Ice Cream And Desert
	("ICD-001", "Vanilla Ice Cream 500ml", "Ice Cream And Desert", 250, 30, "Classic vanilla ice cream."),
	("ICD-002", "Chocolate Ice Cream 500ml", "Ice Cream And Desert", 260, 30, "Rich chocolate ice cream."),

	# Beverages
	("BEV-001", "Mineral Water 1L", "Beverages", 30, 150, "Purified mineral water."),
	("BEV-002", "Cola 500ml", "Beverages", 65, 100, "Carbonated soft drink."),
	("BEV-003", "Orange Juice 1L", "Beverages", 210, 45, "100% orange juice, no added sugar."),
]


def execute():
	if not frappe.db.exists("Franchise", _FRANCHISE["site_code"]):
		frappe.get_doc({"doctype": "Franchise", **_FRANCHISE}).insert(ignore_permissions=True)

	for name in _CATEGORIES:
		if not frappe.db.exists("Saathi Item Category", name):
			frappe.get_doc({
				"doctype": "Saathi Item Category",
				"category_name": name,
			}).insert(ignore_permissions=True)

	franchise = _FRANCHISE["site_code"]
	for item_code, item_name, category, price, stock_qty, description in _ITEMS:
		docname = f"{franchise}-{item_code}"
		if frappe.db.exists("Saathi Item", docname):
			continue
		frappe.get_doc({
			"doctype": "Saathi Item",
			"franchise": franchise,
			"item_code": item_code,
			"item_name": item_name,
			"is_active": 1,
			"category": category,
			"price": price,
			"stock_qty": stock_qty,
			"description": description,
		}).insert(ignore_permissions=True)

	frappe.db.commit()
