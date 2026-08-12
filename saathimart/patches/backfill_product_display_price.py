"""
Patch: backfill Product.display_price / display_compare_price for products
that already existed before these fields were added.

Going forward these stay in sync automatically via
saathimart.events.publisher.on_vendor_listing_changed (Vendor Listing
after_insert/on_update/on_trash) — this patch only needs to run once, to
seed the cache for pre-existing data so it isn't just zero everywhere
until each product's next unrelated Vendor Listing edit.

Run via: bench --site <site> migrate
"""
import frappe


def execute():
    frappe.db.sql("""
        UPDATE `tabProduct` p
        LEFT JOIN (
            SELECT product, MIN(price) AS price, MAX(compare_price) AS compare_price
            FROM `tabVendor Listing`
            WHERE status = 'Active'
            GROUP BY product
        ) vl ON vl.product = p.name
        SET p.display_price = COALESCE(vl.price, 0),
            p.display_compare_price = COALESCE(vl.compare_price, 0)
    """)
