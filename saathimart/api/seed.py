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


def seed_all():
    """Create all demo data."""
    print("Seeding demo data...")

    vendors = seed_vendors()
    print(f"  Created {len(vendors)} vendors")

    categories = seed_categories()
    print(f"  Created {len(categories)} categories")

    products = seed_products(categories)
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
        if frappe.db.exists("Vendor", vdata["name"]):
            vendors.append(vdata["name"])
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
    ]

    for cdata in cat_data:
        if frappe.db.exists("Category", cdata["name"]):
            categories.append(cdata["name"])
            continue

        cat = frappe.new_doc("Category")
        cat.category_name = cdata["category_name"]
        cat.slug = cdata["slug"]
        cat.is_active = 1
        cat.insert(ignore_permissions=True)
        categories.append(cat.name)

    frappe.db.commit()
    return categories


def seed_products(categories):
    """Create 20 demo products."""
    products = []

    product_data = [
        # Dairy & Bakery
        {"name": "prod-milk-1l", "product_name": "Fresh Milk 1L", "category": "cat-dairy", "price": 85, "sku": "MLK-001"},
        {"name": "prod-butter-200g", "product_name": "Amul Butter 200g", "category": "cat-dairy", "price": 120, "sku": "BTR-001"},
        {"name": "prod-bread-white", "product_name": "White Bread 400g", "category": "cat-dairy", "price": 45, "sku": "BRD-001"},
        {"name": "prod-eggs-12", "product_name": "Farm Fresh Eggs (12)", "category": "cat-dairy", "price": 150, "sku": "EGG-001"},
        # Fruits & Vegetables
        {"name": "prod-apple-1kg", "product_name": "Apple 1kg", "category": "cat-fruit", "price": 180, "sku": "APL-001"},
        {"name": "prod-banana-1kg", "product_name": "Banana 1kg", "category": "cat-fruit", "price": 60, "sku": "BNN-001"},
        {"name": "prod-tomato-1kg", "product_name": "Tomato 1kg", "category": "cat-fruit", "price": 50, "sku": "TMT-001"},
        {"name": "prod-potato-1kg", "product_name": "Potato 1kg", "category": "cat-fruit", "price": 40, "sku": "PTT-001"},
        # Snacks & Beverages
        {"name": "prod-chips-lays", "product_name": "Lay's Classic 52g", "category": "cat-snacks", "price": 35, "sku": "CHP-001"},
        {"name": "prod-cola-1l", "product_name": "Coca-Cola 1L", "category": "cat-snacks", "price": 65, "sku": "COL-001"},
        {"name": "prod-biscuit-parle", "product_name": "Parle-G 80g", "category": "cat-snacks", "price": 15, "sku": "BSC-001"},
        {"name": "prod-juice-tropicana", "product_name": "Tropicana 1L", "category": "cat-snacks", "price": 95, "sku": "JUC-001"},
        # Personal Care
        {"name": "prod-shampoo-head-shoulders", "product_name": "Head & Shoulders 180ml", "category": "cat-personal", "price": 175, "sku": "SHP-001"},
        {"name": "prod-soap-dove", "product_name": "Dove Soap 100g", "category": "cat-personal", "price": 45, "sku": "SOP-001"},
        {"name": "prod-toothpaste-colgate", "product_name": "Colgate 150g", "category": "cat-personal", "price": 85, "sku": "TPT-001"},
        {"name": "prod-handwash-dettol", "product_name": "Dettol Handwash 250ml", "category": "cat-personal", "price": 99, "sku": "HDW-001"},
        # Household
        {"name": "prod-detergent-tide", "product_name": "Tide 1kg", "category": "cat-household", "price": 145, "sku": "DET-001"},
        {"name": "prod-dishwash-vim", "product_name": "Vim Dishwash 500ml", "category": "cat-household", "price": 75, "sku": "DSW-001"},
        {"name": "prod-floor-cleaner-lizol", "product_name": "Lizol 500ml", "category": "cat-household", "price": 120, "sku": "FLC-001"},
        {"name": "prod-paper-tissue", "product_name": "Paper Napkins (100)", "category": "cat-household", "price": 55, "sku": "PPR-001"},
    ]

    vendors = frappe.get_all("Vendor", filters={"status": "Active"}, pluck="name")

    for pdata in product_data:
        if frappe.db.exists("Product", pdata["name"]):
            products.append(pdata["name"])
            continue

        product = frappe.new_doc("Product")
        product.product_name = pdata["product_name"]
        product.slug = pdata["name"].replace("prod-", "")
        product.category = pdata["category"]
        product.status = "Active"
        product.price = pdata["price"]
        product.sku = pdata["sku"]
        product.short_description = f"Fresh {pdata['product_name']} from local vendors"
        product.insert(ignore_permissions=True)

        # Create vendor listings for 2-3 random vendors
        num_vendors = random.randint(2, min(3, len(vendors)))
        selected_vendors = random.sample(vendors, num_vendors)

        for i, vendor in enumerate(selected_vendors):
            # Price varies slightly by vendor
            vendor_price = pdata["price"] * random.uniform(0.9, 1.1)

            vl = frappe.new_doc("Vendor Listing")
            vl.product = product.name
            vl.vendor = vendor
            vl.price = round(vendor_price, 2)
            vl.compare_price = round(vendor_price * 1.2, 2) if random.random() > 0.5 else 0
            vl.track_inventory = 1
            vl.available_qty = random.randint(10, 100)
            vl.status = "Active"
            vl.priority = 10 - i  # First vendor gets higher priority
            vl.insert(ignore_permissions=True)

            # Create vendor stock
            vs = frappe.new_doc("Vendor Stock")
            vs.product = product.name
            vs.vendor = vendor
            vs.physical_qty = vl.available_qty
            vs.available_qty = vl.available_qty
            vs.reserved_qty = 0
            vs.insert(ignore_permissions=True)

        products.append(product.name)

    frappe.db.commit()
    return products


def seed_orders(vendors, products, count=50):
    """Create sample orders."""
    orders = []

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
        order.delivery_zone = "zone-kathmandu"
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
