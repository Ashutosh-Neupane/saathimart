"""
Database Index Optimization — add indexes for hot query paths.

These indexes target the most frequent queries in production:
- Vendor Stock lookups (vendor + product)
- Order status queries (customer + status)
- Product search (category + status + name)
- Vendor Listing lookups (product + vendor + status)
- Delivery Slot bookings (slot + date + status)

Run via:
    bench --site saathimart.localhost execute saathimart.api.indexes.add_performance_indexes
"""
import frappe


# Index definitions: (table, columns, unique, comment)
INDEXES = [
    # ── Vendor Stock (queried on every product page + checkout) ──
    ("tabVendor Stock", "vendor, product", False, "Vendor Stock: vendor+product lookup"),
    ("tabVendor Stock", "product, vendor, available_qty", False, "Vendor Stock: product stock query with qty filter"),

    # ── Vendor Listing (queried on every product page) ──
    ("tabVendor Listing", "product, status", False, "Vendor Listing: active listings per product"),
    ("tabVendor Listing", "vendor, status", False, "Vendor Listing: active listings per vendor"),
    ("tabVendor Listing", "barcode", False, "Vendor Listing: barcode lookup"),

    # ── Orders (customer queries + status filters) ──
    ("tabOrder", "customer_email, status", False, "Order: customer order history"),
    ("tabOrder", "status, creation", False, "Order: status-based queries (dashboard)"),
    ("tabOrder", "vendor, status", False, "Order: vendor order management"),

    # ── Order Item (joined with Order frequently) ──
    ("tabOrder Item", "parent, product", False, "Order Item: product lookup per order"),

    # ── Product (search + listing) ──
    ("tabProduct", "status, category", False, "Product: category listing"),
    ("tabProduct", "status, brand", False, "Product: brand filtering"),
    ("tabProduct", "slug", False, "Product: slug-based lookup"),
    ("tabProduct", "status, avg_rating", False, "Product: rating sort"),

    # ── Cart (session-based lookup) ──
    ("tabCart", "session_id", False, "Cart: session lookup"),
    ("tabCart", "customer_email", False, "Cart: customer cart lookup"),

    # ── SM Notification (user inbox) ──
    ("tabSM Notification", "user, `read`", False, "Notification: user inbox query"),
    ("tabSM Notification", "user, creation", False, "Notification: user notifications by date"),

    # ── SM Search Term (analytics) ──
    ("tabSM Search Term", "search_count", False, "Search Term: popular searches"),
    ("tabSM Search Term", "search_key", True, "Search Term: deduplication"),

    # ── Delivery Slot Booking (capacity check) ──
    ("tabDelivery Slot Booking", "slot, delivery_date, status", False, "Slot Booking: capacity check"),

    # ── Webhook Event (event delivery) ──
    ("tabWebhook Event", "status, creation", False, "Webhook Event: pending events"),
    ("tabWebhook Event", "event_type, status", False, "Webhook Event: event type filter"),

    # ── Vendor Stock Ledger Entry (audit trail) ──
    ("tabStock Ledger Entry", "vendor, product, creation", False, "Stock Ledger: vendor stock history"),
]


def add_performance_indexes():
    """Add all performance indexes (idempotent — skips existing)."""
    added = 0
    skipped = 0
    errors = 0

    for table, columns, unique, comment in INDEXES:
        index_name = f"idx_sm_{columns.replace(', ', '_').replace('`', '').replace(' ', '')}"

        # Check if index already exists
        existing = frappe.db.sql(
            "SHOW INDEX FROM `{0}` WHERE Key_name = %s".format(table),
            (index_name,),
        )

        if existing:
            skipped += 1
            continue

        # Check if columns exist in the table
        col_list = [c.strip().strip('`') for c in columns.split(",")]
        existing_cols = frappe.db.sql(
            "SHOW COLUMNS FROM `{0}`".format(table),
            as_dict=True,
        )
        existing_col_names = {c.Field for c in existing_cols}

        missing = [c for c in col_list if c not in existing_col_names]
        if missing:
            frappe.log_error(
                f"Skipping index {index_name} on {table}: missing columns {missing}",
                "index_optimization",
            )
            errors += 1
            continue

        # Create the index
        try:
            unique_str = "UNIQUE " if unique else ""
            col_str = ", ".join(f"`{c.strip()}`" for c in col_list)
            sql = f"CREATE {unique_str}INDEX `{index_name}` ON `{table}` ({col_str})"
            frappe.db.sql(sql)
            added += 1
        except Exception as e:
            # Index might already exist with different name, or table locked
            frappe.log_error(
                f"Failed to create index {index_name}: {e}",
                "index_optimization",
            )
            errors += 1

    frappe.db.commit()
    result = {"added": added, "skipped": skipped, "errors": errors}
    frappe.logger().info(f"Index optimization: {result}")
    return result


def drop_performance_indexes():
    """Drop all performance indexes (for rollback)."""
    dropped = 0
    for table, columns, unique, comment in INDEXES:
        index_name = f"idx_sm_{columns.replace(', ', '_').replace('`', '').replace(' ', '')}"
        try:
            frappe.db.sql(f"DROP INDEX IF EXISTS `{index_name}` ON `{table}`")
            dropped += 1
        except Exception:
            pass
    frappe.db.commit()
    return {"dropped": dropped}
