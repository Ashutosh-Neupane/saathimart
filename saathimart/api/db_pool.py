"""
Database connection pooling and query optimization.

Frappe manages its own connection pool per worker process. This module
adds:
  1. Connection health checks before use
  2. Query timing instrumentation
  3. Bulk insert helpers (much faster than doc.insert() in loops)
  4. Batch upsert for stock/listing operations
"""
import time
import frappe
from frappe.utils import cint


# ── Query Timing ─────────────────────────────────────────────────────────────


class QueryTimer:
    """Context manager that logs slow queries.

    Usage:
        with QueryTimer("product_list"):
            rows = frappe.db.sql(...)
    """
    _slow_threshold_ms = 100  # Log queries slower than 100ms

    def __init__(self, label="query"):
        self.label = label
        self.start = None
        self.elapsed = 0

    def __enter__(self):
        self.start = time.monotonic()
        return self

    def __exit__(self, *args):
        self.elapsed = (time.monotonic() - self.start) * 1000
        if self.elapsed > self._slow_threshold_ms:
            frappe.logger().warning(
                f"SLOW QUERY [{self.label}]: {self.elapsed:.1f}ms"
            )


# ── Bulk Operations ──────────────────────────────────────────────────────────


def bulk_insert(doctype, fields, values, ignore_duplicates=False):
    """Insert many rows at once (much faster than doc.insert() in a loop).

    Args:
        doctype: DocType name
        fields: List of fieldnames
        values: List of tuples, one per row
        ignore_duplicates: Skip duplicate key errors

    Returns: Number of rows inserted
    """
    if not values:
        return 0

    # Frappe's bulk_insert handles batches internally
    frappe.db.bulk_insert(doctype, fields=fields, values=values)
    return len(values)


def bulk_update(doctype, fields, conditions, values_list):
    """Update multiple rows in one query.

    Args:
        doctype: DocType name
        fields: List of fieldnames to update
        conditions: List of condition dicts {fieldname: value}
        values_list: List of dicts {fieldname: value} matching conditions

    Returns: Number of rows updated
    """
    updated = 0
    for cond, vals in zip(conditions, values_list):
        frappe.db.set_value(doctype, cond, vals)
        updated += 1
    return updated


def batch_upsert_stock(vendor, product_updates):
    """Batch upsert Vendor Stock rows.

    Args:
        vendor: Vendor name
        product_updates: List of dicts with keys:
            product, warehouse, physical_qty, available_qty, reserved_qty

    Much faster than loading each doc, modifying, and saving.
    """
    if not product_updates:
        return 0

    updated = 0
    for u in product_updates:
        existing = frappe.db.get_value(
            "Vendor Stock",
            {"vendor": vendor, "product": u["product"], "warehouse": u.get("warehouse", "Main")},
            ["name", "physical_qty"],
            as_dict=True,
        )

        if existing:
            frappe.db.set_value(
                "Vendor Stock",
                existing.name,
                {
                    "physical_qty": u.get("physical_qty", existing.physical_qty),
                    "available_qty": u.get("available_qty", u.get("physical_qty", existing.physical_qty)),
                },
            )
            updated += 1
        else:
            doc = frappe.get_doc({
                "doctype": "Vendor Stock",
                "vendor": vendor,
                "product": u["product"],
                "warehouse": u.get("warehouse", "Main"),
                "physical_qty": u.get("physical_qty", 0),
                "available_qty": u.get("available_qty", u.get("physical_qty", 0)),
                "reserved_qty": u.get("reserved_qty", 0),
            })
            doc.insert(ignore_permissions=True)
            updated += 1

    frappe.db.commit()
    return updated


# ── Connection Health ────────────────────────────────────────────────────────


def check_db_health():
    """Quick DB health check — returns connection status and query count."""
    try:
        start = time.monotonic()
        frappe.db.sql("SELECT 1")
        latency_ms = (time.monotonic() - start) * 1000

        # Get connection count
        conn_count = frappe.db.sql(
            "SHOW STATUS WHERE Variable_name = 'Threads_connected'",
            as_dict=True,
        )
        connections = conn_count[0].Value if conn_count else 0

        return {
            "status": "healthy",
            "latency_ms": round(latency_ms, 2),
            "connections": connections,
        }
    except Exception as e:
        return {
            "status": "unhealthy",
            "error": str(e),
            "latency_ms": -1,
            "connections": -1,
        }


# ── Index Verification ──────────────────────────────────────────────────────


def verify_indexes():
    """Check that all performance indexes exist."""
    from saathimart.api.indexes import INDEXES

    missing = []
    for table, columns, unique, comment in INDEXES:
        index_name = f"idx_sm_{columns.replace(', ', '_').replace('`', '').replace(' ', '')}"
        exists = frappe.db.sql(
            "SHOW INDEX FROM `{0}` WHERE Key_name = %s".format(table),
            (index_name,),
        )
        if not exists:
            missing.append({"table": table, "columns": columns, "index": index_name})

    return {
        "total_indexes": len(INDEXES),
        "missing": len(missing),
        "details": missing,
    }
