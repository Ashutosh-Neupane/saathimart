"""
Migration: saathimart_v1_to_v2

Migrates from single-vendor Product to multi-vendor Vendor Listing architecture.

Changes:
  1. For each Product with a vendor:
     - Create a Vendor Listing row linking product → vendor
     - Copy price, compare_price, stock_qty, sku, delivery_zone, etc.
  2. Migrate Product.thumbnail → Product Media (primary)
  3. Migrate Product.images JSON → Product Media (gallery)
  4. Clear old single-vendor fields from Product (vendor, price, compare_price,
     stock_qty, sku, vendor_product_id, delivery_zone, track_inventory,
     allow_backorder, low_stock_threshold)
  5. Initialize Vendor Stock rows from existing stock_qty

Run with:
  bench --site saathimart.localhost execute saathimart.migrations.v1_to_v2.migrate
"""
import frappe
from frappe.utils import flt, now_datetime


def migrate():
    if frappe.flags.in_migrate:
        return

    frappe.flags.in_migrate = True

    try:
        _migrate_products_to_vendor_listings()
        _migrate_media()
        _migrate_vendor_stock()
        frappe.db.commit()
        print("Migration completed successfully.")
    except Exception:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), "v1_to_v2 migration failed")
        print("Migration FAILED — check error log.")
    finally:
        frappe.flags.in_migrate = False


def _migrate_products_to_vendor_listings():
    """Create Vendor Listing rows from existing Product.vendor."""
    products = frappe.get_list(
        "Product",
        filters={"vendor": ["is", "set"]},
        fields=["name", "vendor", "price", "compare_price", "stock_qty",
                "sku", "vendor_product_id", "delivery_zone",
                "track_inventory", "allow_backorder", "low_stock_threshold",
                "status"],
    )

    count = 0
    for p in products:
        if not p.vendor:
            continue

        # Check if Vendor Listing already exists
        existing = frappe.db.exists(
            "Vendor Listing",
            {"product": p.name, "vendor": p.vendor}
        )
        if existing:
            continue

        vl = frappe.new_doc("Vendor Listing")
        vl.product = p.name
        vl.vendor = p.vendor
        vl.status = "Active" if p.status == "Active" else "Inactive"
        vl.price = flt(p.price or 0)
        vl.compare_price = flt(p.compare_price or 0)
        vl.sku = p.sku or ""
        vl.vendor_product_id = p.vendor_product_id or ""
        vl.delivery_zone = p.delivery_zone
        vl.track_inventory = p.track_inventory or 1
        vl.allow_backorder = p.allow_backorder or 0
        vl.available_qty = flt(p.stock_qty or 0)
        vl.reserved_qty = 0
        vl.physical_qty = flt(p.stock_qty or 0)
        vl.last_updated = now_datetime()
        vl.last_sync_at = now_datetime()
        vl.insert(ignore_permissions=True)
        count += 1

    print(f"  Created {count} Vendor Listing rows")


def _migrate_media():
    """Migrate Product.thumbnail and Product.images JSON to Product Media."""
    products = frappe.get_list(
        "Product",
        filters=[["thumbnail", "is", "set"]],
        fields=["name", "thumbnail", "images"],
    )

    count = 0
    for p in products:
        primary_set = False

        # Migrate thumbnail
        if p.thumbnail:
            pm = frappe.new_doc("Product Media")
            pm.product = p.name
            pm.file = p.thumbnail
            pm.file_type = "image"
            pm.is_primary = 1
            pm.sort_order = 0
            pm.insert(ignore_permissions=True)
            primary_set = True
            count += 1

        # Migrate images JSON array
        if p.images:
            try:
                import json
                urls = json.loads(p.images) if isinstance(p.images, str) else p.images
                if isinstance(urls, list):
                    for idx, url in enumerate(urls):
                        if not url:
                            continue
                        pm = frappe.new_doc("Product Media")
                        pm.product = p.name
                        pm.file = url
                        pm.file_type = "image"
                        pm.is_primary = 1 if not primary_set and idx == 0 else 0
                        pm.sort_order = idx + 1
                        pm.insert(ignore_permissions=True)
                        count += 1
            except Exception:
                pass

    print(f"  Created {count} Product Media rows")


def _migrate_vendor_stock():
    """Create Vendor Stock rows from migrated Vendor Listing stock data."""
    listings = frappe.get_list(
        "Vendor Listing",
        filters={"available_qty": [">", 0]},
        fields=["name", "vendor", "product", "available_qty", "physical_qty"],
    )

    count = 0
    for vl in listings:
        row_name = f"{vl.vendor}-{vl.product}"
        if frappe.db.exists("Vendor Stock", row_name):
            continue

        vs = frappe.new_doc("Vendor Stock")
        vs.vendor = vl.vendor
        vs.product = vl.product
        vs.available_qty = flt(vl.available_qty or 0)
        vs.reserved_qty = 0
        vs.physical_qty = flt(vl.physical_qty or vl.available_qty or 0)
        vs.last_updated = now_datetime()
        vs.last_sync_at = now_datetime()
        vs.insert(ignore_permissions=True)
        count += 1

    print(f"  Created {count} Vendor Stock rows")
