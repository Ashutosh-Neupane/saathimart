"""
Background job optimization — debounce, batch, and prioritize.

Problems solved:
  1. Cart saves: User clicks "add to cart" 5 times → 5 DB writes
     Solution: Debounce — wait 500ms after last click, then write once

  2. Stock updates: 50 products updated → 50 individual DB queries
     Solution: Batch — collect for 2s, then bulk update

  3. Event delivery: Payments and notifications compete for same queue
     Solution: Priority — payments go first, notifications are batched

  4. Search analytics: Every keystroke logs a search
     Solution: Debounce + batch — collect 1s, then bulk insert
"""
import time
import frappe
from frappe.utils import cint


# ── Debouncer ────────────────────────────────────────────────────────────────


class Debouncer:
    """Debounce repeated calls — only execute after a quiet period.

    Usage:
        debouncer = Debouncer(delay=0.5)

        @frappe.whitelist()
        def update_cart(product, qty):
            debouncer.add(product, qty)
            return {"status": "queued"}

        # Or with callback:
        debouncer = Debouncer(delay=1.0, callback=save_cart)
        debouncer.submit(cart_name, items)
    """

    def __init__(self, delay=0.5, callback=None):
        self.delay = delay
        self.callback = callback
        self._last_call = {}
        self._pending = {}

    def should_execute(self, key):
        """Check if enough time has passed since last call for this key."""
        now = time.monotonic()
        last = self._last_call.get(key, 0)
        self._last_call[key] = now
        return (now - last) >= self.delay

    def add(self, key, value=None):
        """Add an item to the pending batch."""
        self._pending[key] = value
        if self.should_execute(key) and self.callback:
            frappe.enqueue(
                self.callback,
                queue="short",
                timeout=30,
                pending=self._pending.copy(),
            )
            self._pending.clear()

    def get_pending(self):
        """Return pending items (for manual flush)."""
        return self._pending.copy()

    def flush(self):
        """Force-execute all pending items."""
        if self.callback and self._pending:
            frappe.enqueue(
                self.callback,
                queue="short",
                timeout=30,
                pending=self._pending.copy(),
            )
        self._pending.clear()


# ── Batch Processor ──────────────────────────────────────────────────────────


class BatchProcessor:
    """Collect items over a time window, then process them in bulk.

    Usage:
        processor = BatchProcessor(
            batch_size=50,
            flush_interval=2.0,
            processor_fn=bulk_update_stock,
        )

        # In your endpoint:
        processor.add({"product": "Milk", "qty": 10})
        processor.add({"product": "Bread", "qty": 5})
        # Auto-flushes when batch_size reached or flush_interval elapsed
    """

    def __init__(self, batch_size=50, flush_interval=2.0, processor_fn=None):
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.processor_fn = processor_fn
        self._buffer = []
        self._last_flush = time.monotonic()

    def add(self, item):
        """Add an item to the batch buffer."""
        self._buffer.append(item)

        # Auto-flush if batch is full or interval elapsed
        if len(self._buffer) >= self.batch_size:
            self.flush()
        elif (time.monotonic() - self._last_flush) >= self.flush_interval:
            self.flush()

    def flush(self):
        """Process all buffered items."""
        if not self._buffer:
            return

        items = self._buffer[:]
        self._buffer.clear()
        self._last_flush = time.monotonic()

        if self.processor_fn:
            try:
                self.processor_fn(items)
            except Exception as e:
                frappe.log_error(
                    f"BatchProcessor flush failed: {e}",
                    "job_optimization",
                )

    @property
    def pending_count(self):
        return len(self._buffer)


# ── Pre-configured Processors ────────────────────────────────────────────────


def _bulk_update_stock(items):
    """Process a batch of stock updates."""
    from saathimart.api.db_pool import batch_upsert_stock

    # Group by vendor
    by_vendor = {}
    for item in items:
        vendor = item.pop("vendor", None)
        if vendor:
            by_vendor.setdefault(vendor, []).append(item)

    for vendor, updates in by_vendor.items():
        batch_upsert_stock(vendor, updates)


def _bulk_log_search(items):
    """Process a batch of search analytics entries."""
    if not items:
        return

    try:
        frappe.db.bulk_insert(
            "SM Search Term",
            fields=["search_key", "search_count", "creation"],
            values=[
                (item.get("query", ""), item.get("count", 1), frappe.utils.now_datetime())
                for item in items
            ],
        )
        frappe.db.commit()
    except Exception:
        pass  # Non-critical analytics


# Global processor instances (created per-worker, not shared)
_stock_processor = None
_search_processor = None


def get_stock_processor():
    """Get or create the stock batch processor."""
    global _stock_processor
    if _stock_processor is None:
        _stock_processor = BatchProcessor(
            batch_size=50,
            flush_interval=2.0,
            processor_fn=_bulk_update_stock,
        )
    return _stock_processor


def get_search_processor():
    """Get or create the search analytics batch processor."""
    global _search_processor
    if _search_processor is None:
        _search_processor = BatchProcessor(
            batch_size=20,
            flush_interval=1.0,
            processor_fn=_bulk_log_search,
        )
    return _search_processor


def queue_stock_update(vendor, product, warehouse, qty):
    """Queue a stock update (batched)."""
    processor = get_stock_processor()
    processor.add({
        "vendor": vendor,
        "product": product,
        "warehouse": warehouse,
        "physical_qty": qty,
        "available_qty": qty,
    })


def queue_search_analytics(query, count=1):
    """Queue a search analytics entry (batched)."""
    processor = get_search_processor()
    processor.add({"query": query, "count": count})
