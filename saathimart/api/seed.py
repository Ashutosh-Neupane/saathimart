"""
Demo seed script — creates sample data for local development and testing.

Creates:
    - 5 vendors with warehouses
    - 20 products across categories
    - 50 sample orders
    - Customer data

Usage:
    bench --site saathimart.localhost execute saathimart.api.seed.seed_all
    bench --site saathimart.localhost execute saathimart.api.seed.seed_products
    bench --site saathimart.localhost execute saathimart.api.seed.seed_orders
"""
import random
from datetime import datetime, timedelta

import frappe
from frappe.utils import flt, today, add_days, now_datetime


def seed_all(count=200):
    """Create all demo data."""
    print("Seeding demo data...")

    vendors = seed_vendors()
    print(f"  Created {len(vendors)} vendors")

    categories = seed_categories()
    print(f"  Created {len(categories)} categories")

    products = seed_products(categories, count=count)
    print(f"  Created {len(products)} products")

    orders = seed_orders(vendors, products)
    print(f"  Created {len(orders)} orders")

    print("Seeding complete!")
    return {"vendors": len(vendors), "products": len(products), "orders": len(orders)}


def seed_vendors():
    """Create 5 demo vendors with warehouses."""
    vendors = []

    vendor_data = [
        {"name": "vendor-kathmandu", "vendor_name": "Kathmandu Fresh Mart", "lat": 27.7172, "lng": 85.3240},
        {"name": "vendor-pokhara", "vendor_name": "Pokhara Grocery Hub", "lat": 28.2096, "lng": 83.9856},
        {"name": "vendor-chitwan", "vendor_name": "Chitwan Daily Needs", "lat": 27.5291, "lng": 84.3542},
        {"name": "vendor-lalitpur", "vendor_name": "Lalitpur Organic Store", "lat": 27.6644, "lng": 85.3188},
        {"name": "vendor-bhaktapur", "vendor_name": "Bhaktapur Local Market", "lat": 27.6710, "lng": 85.4298},
    ]

    for vdata in vendor_data:
        # Vendor autonames by hash with a unique `slug` (auto-derived from
        # vendor_name), so idempotency must key on slug — checking by name
        # always missed existing rows and crashed re-seeds with a
        # UniqueValidationError on `slug`.
        slug = frappe.scrub(vdata["vendor_name"]).replace("_", "-")
        existing = frappe.db.exists("Vendor", {"slug": slug})
        if existing:
            vendors.append(existing)
            continue

        vendor = frappe.new_doc("Vendor")
        vendor.vendor_name = vdata["vendor_name"]
        vendor.status = "Active"
        vendor.lat = vdata["lat"]
        vendor.lng = vdata["lng"]
        vendor.service_radius_km = 10
        vendor.contact_email = f"demo@{vdata['name'].replace('vendor-', '')}.com"
        vendor.contact_phone = f"98{random.randint(10000000, 99999999)}"
        vendor.insert(ignore_permissions=True)

        # Create default warehouse as child table entry
        vendor.append("warehouses", {
            "warehouse_name": f"{vdata['vendor_name']} - Main",
            "is_default": 1,
            "lat": vdata["lat"],
            "lng": vdata["lng"],
        })
        vendor.save(ignore_permissions=True)

        vendors.append(vendor.name)

    frappe.db.commit()
    return vendors


def seed_categories():
    """Create demo categories."""
    categories = []

    cat_data = [
        {"name": "cat-dairy", "category_name": "Dairy & Bakery", "slug": "dairy-bakery"},
        {"name": "cat-fruit", "category_name": "Fruits & Vegetables", "slug": "fruits-vegetables"},
        {"name": "cat-snacks", "category_name": "Snacks & Beverages", "slug": "snacks-beverages"},
        {"name": "cat-personal", "category_name": "Personal Care", "slug": "personal-care"},
        {"name": "cat-household", "category_name": "Household Essentials", "slug": "household-essentials"},
        {"name": "cat-staples", "category_name": "Staples", "slug": "staples"},
    ]

    for cdata in cat_data:
        # Category autonames by field:slug, so the row's name is the slug
        # ("dairy-bakery"), never the legacy "cat-dairy" — idempotency must
        # key on slug or every re-seed dies on the unique slug.
        existing = frappe.db.exists("Category", {"slug": cdata["slug"]})
        if existing:
            categories.append(existing)
            continue

        cat = frappe.new_doc("Category")
        cat.category_name = cdata["category_name"]
        cat.slug = cdata["slug"]
        cat.is_active = 1
        cat.insert(ignore_permissions=True)
        categories.append(cat.name)

    frappe.db.commit()
    return categories


def seed_products(categories, count=200):
    """Create demo products with per-vendor listings AND per-vendor stock.

    Stock truth lives on Vendor Stock (checkout reserves from it);
    Vendor Listing.available_qty is display-only. Each product is listed
    by 1-3 active vendors with its own price and stock pool so the
    marketplace-wide total on browse cards aggregates real per-vendor rows.
    """
    products = []

    # Realistic mart catalogue: base items expanded into pack-size variants.
    base_items = [
        # (name, category, base price NPR, unit)
        ("Fresh Milk", "dairy-bakery", 85, "1L"),
        ("Amul Butter", "dairy-bakery", 120, "200g"),
        ("White Bread", "dairy-bakery", 45, "400g"),
        ("Farm Fresh Eggs", "dairy-bakery", 150, "(12)"),
        ("Yogurt", "dairy-bakery", 60, "500g"),
        ("Cheese Slices", "dairy-bakery", 180, "(10)"),
        ("Paneer", "dairy-bakery", 160, "200g"),
        ("Apple", "fruits-vegetables", 180, "1kg"),
        ("Banana", "fruits-vegetables", 60, "1kg"),
        ("Tomato", "fruits-vegetables", 50, "1kg"),
        ("Potato", "fruits-vegetables", 40, "1kg"),
        ("Onion", "fruits-vegetables", 55, "1kg"),
        ("Carrot", "fruits-vegetables", 70, "1kg"),
        ("Cabbage", "fruits-vegetables", 45, "pc"),
        ("Orange", "fruits-vegetables", 120, "1kg"),
        ("Grapes", "fruits-vegetables", 220, "500g"),
        ("Mango", "fruits-vegetables", 250, "1kg"),
        ("Lay's Classic", "snacks-beverages", 35, "52g"),
        ("Coca-Cola", "snacks-beverages", 65, "1L"),
        ("Parle-G", "snacks-beverages", 15, "80g"),
        ("Tropicana", "snacks-beverages", 95, "1L"),
        ("Sprite", "snacks-beverages", 65, "1L"),
        ("Fanta", "snacks-beverages", 65, "1L"),
        ("Instant Noodles", "snacks-beverages", 30, "70g"),
        ("Head & Shoulders", "personal-care", 175, "180ml"),
        ("Dove Soap", "personal-care", 45, "100g"),
        ("Colgate", "personal-care", 85, "150g"),
        ("Dettol Handwash", "personal-care", 99, "250ml"),
        ("Lifebuoy Soap", "personal-care", 40, "100g"),
        ("Patanjali Toothpaste", "personal-care", 75, "100g"),
        ("Nivea Cream", "personal-care", 190, "100ml"),
        ("Tide", "household-essentials", 145, "1kg"),
        ("Vim Dishwash", "household-essentials", 75, "500ml"),
        ("Lizol", "household-essentials", 120, "500ml"),
        ("Paper Napkins", "household-essentials", 55, "(100)"),
        ("Harpic", "household-essentials", 130, "500ml"),
        ("Surf Excel", "household-essentials", 155, "1kg"),
        ("Colin Glass Cleaner", "household-essentials", 85, "250ml"),
        ("Basmati Rice", "staples", 320, "5kg"),
        ("Sunflower Oil", "staples", 280, "1L"),
        ("Red Lentils", "staples", 150, "1kg"),
        ("Chickpeas", "staples", 140, "1kg"),
        ("Sugar", "staples", 90, "1kg"),
        ("Salt", "staples", 25, "1kg"),
        ("Tea Leaves", "staples", 240, "500g"),
        ("Coffee Powder", "staples", 350, "200g"),
        ("Chiwda", "snacks-beverages", 45, "200g"),
        ("Churpi", "snacks-beverages", 60, "100g"),
        ("Sel Roti Mix", "staples", 120, "1kg"),
        ("Momo", "dairy-bakery", 180, "(10 pc frozen)"),
    ]

    # Expand to ~200 unique products: base item × pack variant where useful.
    product_data = []
    seen = set()
    for name, cat, price, unit in base_items:
        for variant, mult in (("", 1.0), (" Family Pack", 2.5), (" Combo", 4.0)):
            if len(product_data) >= count:
                break
            pname = f"{name} {unit}{variant}".strip()
            if pname in seen:
                continue
            seen.add(pname)
            product_data.append({
                "product_name": pname,
                "category": cat,
                "price": round(price * mult, 0),
            })
        if len(product_data) >= count:
            break

    # Top up with numbered staples if still short (idempotent-safe names).
    i = 1
    while len(product_data) < count:
        pname = f"Mart Essentials Combo #{i}"
        if pname not in seen:
            seen.add(pname)
            product_data.append({"product_name": pname, "category": "household-essentials", "price": 100 + i * 5})
        i += 1

    vendors = frappe.get_all("Vendor", filters={"status": "Active"}, pluck="name")
    if not vendors:
        print("  No active vendors — skipping product seeding")
        return products

    for pdata in product_data:
        # Product autonames by field:product_name (with -N suffixes for
        # duplicates) — key idempotency on product_name to keep re-seeds
        # from piling up "...-1" copies.
        existing = frappe.db.exists("Product", {"product_name": pdata["product_name"]})
        if existing:
            products.append(existing)
            continue

        product = frappe.new_doc("Product")
        product.product_name = pdata["product_name"]
        product.slug = frappe.scrub(pdata["product_name"]).replace("_", "-")
        product.category = pdata["category"]
        product.status = "Active"
        # NOTE: Product.price is a read-only computed property (min price
        # across active Vendor Listings). The seed price lives on the
        # Vendor Listing created below — setting it here raised
        # AttributeError after the listing-based pricing refactor.
        product.sku = f"SM-{random.randint(100000, 999999)}"
        product.short_description = f"Fresh {pdata['product_name']} from local vendors"
        product.insert(ignore_permissions=True)

        # 1-3 vendors list each product; stock truth goes on Vendor Stock.
        num_vendors = random.randint(1, min(3, len(vendors)))
        selected_vendors = random.sample(vendors, num_vendors)

        for i, vendor in enumerate(selected_vendors):
            vendor_price = pdata["price"] * random.uniform(0.9, 1.1)
            qty = random.randint(15, 120)

            vl = frappe.new_doc("Vendor Listing")
            vl.product = product.name
            vl.vendor = vendor
            vl.price = round(vendor_price, 2)
            vl.compare_price = round(vendor_price * 1.2, 2) if random.random() > 0.5 else 0
            vl.track_inventory = 1
            vl.status = "Active"
            vl.priority = 10 - i
            vl.warehouse = "default"
            vl.insert(ignore_permissions=True)

            # Stock truth: one default-warehouse pool per vendor+product.
            vs = frappe.new_doc("Vendor Stock")
            vs.product = product.name
            vs.vendor = vendor
            vs.warehouse = "default"
            vs.is_default_warehouse = 1
            vs.physical_qty = qty
            vs.available_qty = qty
            vs.reserved_qty = 0
            vs.insert(ignore_permissions=True)

        products.append(product.name)

    frappe.db.commit()
    return products


def seed_orders(vendors, products, count=50):
    """Create sample orders."""
    orders = []

    # seed_orders (and checkout) link a Delivery Zone; nothing else in the
    # seed flow created one. Delivery Zone autonames by field:zone_name, so
    # the row's primary key IS "Kathmandu" — resolve the real name instead
    # of assuming a "zone-kathmandu" slug.
    if not frappe.db.exists("Delivery Zone", {"zone_name": "Kathmandu"}):
        zone_fields = [f.fieldname for f in frappe.get_meta("Delivery Zone").fields]
        zone = {"doctype": "Delivery Zone", "zone_name": "Kathmandu", "is_active": 1}
        if "delivery_charge" in zone_fields:
            zone["delivery_charge"] = 100
        if "base_delivery_charge" in zone_fields:
            zone["base_delivery_charge"] = 100
        if "latitude" in zone_fields:
            zone["latitude"] = 27.7172
        if "longitude" in zone_fields:
            zone["longitude"] = 85.3240
        if "radius_km" in zone_fields:
            zone["radius_km"] = 10
        zdoc = frappe.get_doc(zone)
        zdoc.flags.ignore_permissions = True
        zdoc.flags.ignore_links = True
        try:
            zdoc.insert()
            frappe.db.commit()
        except Exception:
            frappe.db.rollback()

    customer_names = [
        "Ram Shrestha", "Sita Gurung", "Hari Thapa", "Gita Magar",
        "Krishna Tamang", "Laxmi Rai", "Shyam Poudel", "Sarita Karki",
        "Bishnu Adhikari", "Anita Bhandari", "Rajeshwor Singh", "Mina Koirala",
    ]

    statuses = ["Pending", "Confirmed", "Preparing", "Out for Delivery", "Delivered"]
    payment_methods = ["COD", "eSewa"]

    for i in range(count):
        # Random customer
        customer_name = random.choice(customer_names)
        customer_phone = f"98{random.randint(10000000, 99999999)}"

        # Random products (1-5 items)
        num_items = random.randint(1, 5)
        selected_products = random.sample(products, min(num_items, len(products)))

        # Pick a vendor for this order
        vendor = random.choice(vendors)

        # Create order
        order = frappe.new_doc("Order")
        order.customer_name = customer_name
        order.customer_phone = customer_phone
        order.customer_email = f"{customer_name.lower().replace(' ', '.')}@demo.com"
        order.delivery_address = f"House {random.randint(1, 100)}, Kathmandu, Nepal"
        order.delivery_zone = frappe.db.get_value(
            "Delivery Zone", {"zone_name": "Kathmandu"}, "name"
        )
        order.payment_method = random.choice(payment_methods)
        order.status = random.choice(statuses)
        order.payment_status = "Paid" if order.status in ["Delivered", "Preparing"] else "Unpaid"
        order.vendor = vendor
        order.notes = f"Demo order #{i+1}"

        # Add items
        grand_total = 0
        for prod_name in selected_products:
            product_doc = frappe.get_doc("Product", prod_name)
            # Get price from vendor listing
            vl = frappe.db.get_value(
                "Vendor Listing",
                {"product": prod_name, "vendor": vendor, "status": "Active"},
                ["price"],
            )
            price = flt(vl) if vl else flt(product_doc.price)
            qty = random.randint(1, 3)
            amount = price * qty
            grand_total += amount

            order.append("items", {
                "product": prod_name,
                "product_name": product_doc.product_name,
                "qty": qty,
                "rate": price,
                "amount": amount,
                "vendor": vendor,
            })

        order.grand_total = grand_total

        # Set creation date to last 7 days for realistic trends
        days_ago = random.randint(0, 6)
        order.insert(ignore_permissions=True)

        # Update creation date
        creation_date = add_days(now_datetime(), -days_ago)
        frappe.db.set_value("Order", order.name, "creation", creation_date)

        orders.append(order.name)

    frappe.db.commit()
    return orders


def clear_all():
    """Clear all demo data (use with caution!)."""
    print("Clearing all demo data...")

    # Delete in reverse order of dependencies
    frappe.db.sql("DELETE FROM `tabOrder Item` WHERE parent LIKE 'ORD-%'")
    frappe.db.sql("DELETE FROM `tabOrder` WHERE name LIKE 'ORD-%'")
    frappe.db.sql("DELETE FROM `tabVendor Stock` WHERE product LIKE 'prod-%'")
    frappe.db.sql("DELETE FROM `tabVendor Listing` WHERE product LIKE 'prod-%'")
    frappe.db.sql("DELETE FROM `tabProduct` WHERE name LIKE 'prod-%'")
    # Vendor Warehouse is a child table — use parent column, not vendor
    frappe.db.sql("DELETE FROM `tabVendor Warehouse` WHERE parent LIKE 'vendor-%'")
    frappe.db.sql("DELETE FROM `tabVendor` WHERE name LIKE 'vendor-%'")
    frappe.db.sql("DELETE FROM `tabCategory` WHERE name LIKE 'cat-%'")

    frappe.db.commit()
    print("All demo data cleared")
