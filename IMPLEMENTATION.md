# SaathiMart — Multi-Vendor Implementation Plan

## Overview

Two GitHub repositories working together:

- **Repo 1 — `saathimart`** — Central hub. Customer-facing API. Owns the product
  catalogue, orders, cart, payments, loyalty, stock ledger, vendor registry, location engine.
- **Repo 2 — `saathimart-vendor`** — Frappe app installed on each vendor's existing
  ERPNext site. Hooks into ERPNext Stock Ledger Entry, Sales Order, Delivery Note,
  Purchase Receipt. Maintains a local outbox so no event is ever lost even when the
  hub is down.

---

## Product Mapping — How It Works

This is the core question. Vendor A calls rice "RICE-5KG-A". Vendor B calls it
"ITM-00042". SaathiMart hub calls it "SM-PROD-001". The physical barcode on the bag
is 8901234567890. The barcode is the only thing all three agree on.

### The mapping table (lives on vendor ERPNext, managed by saathimart-vendor app)

```
SM Product Mapping (DocType on vendor ERPNext)
  barcode          8901234567890        ← physical barcode, the shared key
  vendor_item_code RICE-5KG-A           ← vendor's own ERPNext item_code
  hub_sku          SKU-RICE-5KG         ← hub's canonical SKU
  hub_product_id   SM-PROD-001          ← hub's document name
  vendor_id        vendor-a             ← which vendor this mapping belongs to
  last_synced      2024-01-15 10:30:00  ← when hub last confirmed this mapping
  is_active        1
```

### How a mapping gets created (three ways)

**Way 1 — Admin maps manually on vendor ERPNext desk**
Vendor staff opens SM Product Mapping, types their item_code, scans barcode,
hits "Sync with Hub". App calls hub API, hub returns hub_product_id, mapping saved.

**Way 2 — Barcode scan at vendor warehouse**
Staff scans barcode on incoming goods. saathimart-vendor app calls:
`GET /api/method/saathimart.api.products.lookup_by_barcode?barcode=8901234567890`
Hub returns product details. App creates mapping automatically.

**Way 3 — Bulk import**
Vendor exports their item list as CSV (item_code, barcode).
saathimart-vendor app has an import tool that calls hub for each barcode,
creates mappings in bulk.

### Lookup order when vendor pushes a stock event

```
Vendor pushes: { barcode: "8901234567890", qty_change: -5, item_code: "RICE-5KG-A" }

Hub resolution:
  Step 1 → look up Product Barcode Mapping WHERE barcode = "8901234567890"
           → found: hub_product_id = SM-PROD-001  ✓ done

  Step 2 (fallback) → look up Product WHERE sku = vendor's item_code
           → if found ✓ done, also create barcode mapping for next time

  Step 3 (fallback) → look up Product WHERE name = vendor's item_code
           → if found ✓ done

  Step 4 (no match) → log as Unmapped Product event
           → SM Admin alerted: "Vendor A pushed stock for unknown barcode 8901234567890"
           → event held in Dead state until admin maps it
           → admin maps it → re-queue event → processes normally
```

---

## Stock Model — VendorStock Table

Instead of one `Product.stock_qty` field, the hub maintains per-vendor stock:

```
VendorStock (DocType on hub)
  vendor          vendor-a
  product         SM-PROD-001
  available_qty   45    ← what customers can buy RIGHT NOW
  reserved_qty    5     ← held by pending orders not yet confirmed by vendor
  physical_qty    50    ← available_qty + reserved_qty (actual warehouse qty)
  last_updated    timestamp
  last_sync_at    timestamp  ← when vendor last pushed a stock event
```

### What customers see

```
available_qty > 0   → "In Stock"
available_qty <= 0  → "Out of Stock"
available_qty < 3   → "Only X left"
```

### The atomic reservation (prevents oversell)

At checkout, instead of a normal read-then-write, hub runs one atomic SQL UPDATE:

```sql
UPDATE `tabVendor Stock`
SET
  available_qty = available_qty - %(qty)s,
  reserved_qty  = reserved_qty  + %(qty)s
WHERE
  vendor  = %(vendor)s
  AND product = %(product)s
  AND available_qty >= %(qty)s
```

If `ROW_COUNT() == 0` → someone else grabbed the last unit → throw "Just sold out,
please try another vendor". Hub then re-runs location query to find next nearest
vendor with stock and suggests it to customer.

---

## Location Model — VendorLocation Table

```
VendorLocation (DocType on hub)
  vendor              vendor-a
  lat                 27.7172
  lng                 85.3240
  service_radius_km   5.0
  address             Thamel, Kathmandu
  is_active           1
```

### Nearest vendor query

```
Customer at lat=27.720, lng=85.321 wants SM-PROD-001

SELECT
  vs.vendor,
  vs.available_qty,
  (6371 * acos(
    cos(radians(%(lat)s)) * cos(radians(vl.lat)) *
    cos(radians(vl.lng) - radians(%(lng)s)) +
    sin(radians(%(lat)s)) * sin(radians(vl.lat))
  )) AS distance_km
FROM `tabVendor Stock` vs
JOIN `tabVendor Location` vl ON vl.vendor = vs.vendor
WHERE
  vs.product = %(product)s
  AND vs.available_qty > 0
  AND vl.is_active = 1
HAVING distance_km <= vl.service_radius_km
ORDER BY distance_km ASC
LIMIT 5
```

Returns: nearest vendor first, with distance and available qty.
Customer sees: "Vendor A — 0.3 km away — 45 in stock"

---

## Race Conditions — Every Scenario Solved

### Scenario 1 — Customer and shop staff buy last unit simultaneously

```
T=0ms  VendorStock: available_qty = 1

T=0ms  Customer hits checkout on SaathiMart app
T=0ms  Shop staff submits Sales Invoice on ERPNext counter

T=1ms  SaathiMart atomic UPDATE:
         available_qty = 1 - 1 = 0, reserved_qty = 0 + 1 = 1
         WHERE available_qty >= 1  → ROW_COUNT = 1  ✓ reservation wins

T=1ms  ERPNext Sales Invoice submitted
         → stock deducted locally on ERPNext (physical = 0)
         → saathimart-vendor hook writes to SM Sync Outbox:
           { event: stock.deduct, qty_change: -1, barcode: X }

T=2ms  Outbox flush pushes to hub:
         hub runs: available_qty = 0 - 1 = -1
         hub detects available_qty < 0
         → creates "Stock Conflict" alert for SM Admin
         → vendor must resolve:
             Option A: vendor confirms they can still fulfil SaathiMart order
                       (they had more physical stock than hub knew about)
                       → vendor pushes stock.receipt to correct balance
             Option B: vendor cannot fulfil
                       → pushes order.cancel to hub
                       → hub cancels order, releases reservation
                       → customer refunded if paid online
                       → customer notified: "Sorry, just sold out"
```

### Scenario 2 — Two customers buy last unit on SaathiMart simultaneously

```
T=0ms  available_qty = 1

T=0ms  Customer A checkout → atomic UPDATE WHERE available_qty >= 1
T=0ms  Customer B checkout → atomic UPDATE WHERE available_qty >= 1

Database serialises these two writes. One wins, one gets ROW_COUNT = 0.

Winner (Customer A): reservation succeeds → order created
Loser  (Customer B): ROW_COUNT = 0 → hub throws ValidationError
                     → "This item just sold out. Here are nearby vendors
                        that still have stock:" → re-runs location query
                     → Customer B picks next nearest vendor
```

### Scenario 3 — Vendor pushes wrong stock number

```
Vendor accidentally pushes qty_change = +500 instead of +5

Hub applies it → available_qty jumps to 550
Customers can now buy 550 units that don't exist

Prevention:
  Hub validates: if qty_change > max_single_receipt_qty (configurable, default 1000)
                 → reject with error, alert SM Admin

Detection (hourly reconciliation):
  saathimart-vendor compares ERPNext actual stock vs hub VendorStock
  If difference > reconciliation_threshold (default 10%)
  → creates Adjustment SLE on hub to correct balance
  → logs reconciliation report
```

### Scenario 4 — Vendor cancels order after hub already confirmed it

```
Hub Order status = Confirmed, reserved_qty = 5

Vendor pushes order.cancel to hub

Hub:
  1. Releases reservation: reserved_qty -= 5, available_qty += 5
  2. Order status → Cancelled
  3. If payment_status = Paid → triggers refund flow
  4. Checks if another vendor has same product in stock nearby
  5. If yes → notifies customer: "Your vendor cancelled. Reassign to Vendor B?"
  6. If no  → notifies customer: "Out of stock, full refund issued"
```

---

## Queue and Failure Handling

### Hub → Vendor (outbound queue)

Hub uses the existing Webhook Event table with this retry ladder:

```
Attempt 1 → immediate
Attempt 2 → 2 min later
Attempt 3 → 4 min later
Attempt 4 → 8 min later
Attempt 5 → 16 min later
After 5 fails → status = Dead → SM Admin email alert

Dead event options for SM Admin:
  a. Re-queue manually (vendor site back online)
  b. Reassign order to another vendor with same barcode
  c. Cancel order → customer refunded
  d. Mark as manually handled (staff called vendor by phone)
```

### Vendor → Hub (outbound outbox on vendor ERPNext)

saathimart-vendor app uses the Transactional Outbox Pattern:

```
When ERPNext stock changes (Sales Invoice, Purchase Receipt, etc.):
  Step 1: ERPNext writes its own Stock Ledger Entry  ← normal ERPNext flow
  Step 2: saathimart-vendor hook writes SM Sync Outbox row
          SAME database transaction as step 1
          → if step 1 succeeds, step 2 always succeeds
          → event can NEVER be lost even if hub is down

SM Sync Outbox row:
  event_type   stock.deduct
  payload      { barcode, qty_change, voucher_no, vendor_id }
  status       Pending
  retry_count  0
  created_at   timestamp

Background job (every 1 minute):
  SELECT all Pending rows ORDER BY created_at ASC
  For each row:
    POST to hub → if 200 → status = Sent
               → if error → retry_count += 1
               → if retry_count > 10 → status = Dead → alert vendor admin
```

### What happens when hub is down

```
Vendor ERPNext sells 20 items over 2 hours while hub is down

All 20 stock events written to SM Sync Outbox (status=Pending)
Hub VendorStock is stale (shows more than reality)
Customers may oversell during this window

When hub comes back:
  Outbox flushes all 20 events in order
  Hub applies each one sequentially
  If available_qty goes negative → Stock Conflict alert
  Admin reviews and resolves each conflict

Prevention (best effort):
  Hub health check endpoint: GET /api/method/ping
  saathimart-vendor checks every 5 min
  If hub unreachable for > 15 min → vendor admin alerted:
    "SaathiMart hub is unreachable. Stock shown on customer app
     may be inaccurate. Outbox is holding X events."
```

### What happens when vendor site is down

```
Customer places order on SaathiMart
Hub tries to push order.new to vendor ERPNext → fails

Hub behaviour:
  Retry ladder runs (up to 5 attempts over ~30 min)
  Customer sees: "Order confirmed, awaiting vendor acceptance"
  After 30 min no response → SM Admin alerted
  Admin options:
    a. Reassign to another vendor with same barcode nearby
    b. Cancel order → refund
    c. Mark as manually confirmed (called vendor by phone)

Order status during this window: Pending (not Confirmed)
Customer is NOT shown "Confirmed" until vendor actually responds
```

---

## Hourly Stock Reconciliation

Every hour, saathimart-vendor runs a reconciliation job:

```
For each mapped product:
  1. Get actual ERPNext stock (bin.actual_qty for default warehouse)
  2. Get hub VendorStock.physical_qty for this vendor+product
  3. Calculate drift = actual - physical_qty

  If abs(drift) > reconciliation_threshold (default: 2 units or 5%):
    Push stock.adjustment to hub:
      { barcode, qty_change: drift, voucher_type: Adjustment,
        voucher_no: RECON-<date>, remarks: "Hourly reconciliation" }
    Hub creates SLE (voucher_type=Adjustment)
    Hub updates VendorStock.available_qty += drift
    Hub logs reconciliation result

  If drift == 0: log "OK, no drift"
```

This is the safety net that catches everything the real-time sync missed.

---

## Repo 1 — `saathimart` (Central Hub) — Full Spec

### New Doctypes

#### VendorStock
```
Fields:
  vendor          Link → Vendor       (reqd, in_list_view)
  product         Link → Product      (reqd, in_list_view)
  available_qty   Float  default 0    (what customers can buy)
  reserved_qty    Float  default 0    (held by pending orders)
  physical_qty    Float  default 0    (available + reserved, actual warehouse)
  last_updated    Datetime read_only
  last_sync_at    Datetime read_only  (when vendor last pushed)

Unique constraint: vendor + product (one row per vendor per product)
Permissions: SM Admin rw, SM Vendor r
```

#### VendorLocation
```
Fields:
  vendor              Link → Vendor   (reqd, unique)
  lat                 Float           (reqd)
  lng                 Float           (reqd)
  service_radius_km   Float default 5
  address             Small Text
  is_active           Check default 1

Permissions: SM Admin rw, Guest r
```

#### Product Barcode Mapping
```
Fields:
  barcode         Data    (reqd, unique) ← the shared physical barcode
  product         Link → Product (reqd)
  sku             Data    (fetched from product.sku)
  is_active       Check default 1
  notes           Small Text

Purpose: barcode → hub product lookup for all vendor pushes
Permissions: SM Admin rw
```

### New/Updated API Files

#### `api/location.py` (new)
```python
# Endpoints:
resolve_vendors(lat, lng, radius_km=5)
  → returns vendors within radius sorted by distance
  → each vendor includes: distance_km, available products count

nearest_vendor_for_product(product, lat, lng)
  → returns ordered list of vendors that have product in stock
  → sorted by distance, filtered by service_radius_km
  → used at checkout to auto-assign vendor
```

#### `api/stock.py` (new)
```python
# Endpoints:
atomic_reserve(vendor, product, qty)
  → runs the atomic SQL UPDATE
  → returns { ok, new_available_qty } or raises ValidationError

release_reservation(vendor, product, qty, order_id)
  → called on order cancel
  → reserved_qty -= qty, available_qty += qty

confirm_deduction(vendor, product, qty, order_id)
  → called when vendor confirms delivery
  → reserved_qty -= qty, physical_qty -= qty
  → creates SLE (voucher_type=Order)

apply_vendor_stock_event(payload)
  → resolves product by barcode → fallback to sku → fallback to name
  → updates VendorStock
  → creates SLE
  → if available_qty < 0 → creates Stock Conflict alert
```

#### `api/products.py` (updated)
```python
list_products(category, vendor, search, lat, lng, radius_km, page, page_size)
  → when lat/lng provided: joins VendorStock + VendorLocation
  → filters: available_qty > 0, vendor within radius
  → returns product + nearest_vendor + distance_km + available_qty

lookup_by_barcode(barcode)
  → looks up Product Barcode Mapping
  → returns product details for vendor mapping creation
```

#### `api/cart.py` (updated)
```python
add_to_cart(session_id, product, qty, vendor=None, lat=None, lng=None)
  → if vendor not provided and lat/lng provided:
      auto-resolve nearest vendor with stock
  → cart item now carries vendor field
  → validates vendor has available_qty >= qty at add time
    (soft check only — hard check is at checkout atomic reserve)
```

#### `api/orders.py` (updated)
```python
checkout(...)
  → for each cart item:
      calls atomic_reserve(item.vendor, item.product, item.qty)
      if any reservation fails → rollback all previous reservations
      → throw "X just sold out at this vendor. Try [next nearest vendor]"
  → after order insert:
      enqueues order.new webhook to vendor site
      (uses existing Webhook Event queue with retry ladder)
```

#### `api/events.py` (updated)
```python
receive(event, payload)
  → stock.receipt / stock.deduct / stock.adjustment:
      calls apply_vendor_stock_event(payload)
      resolves by barcode first, then sku, then name
      if no match → logs Unmapped Product, holds event, alerts admin

  → order.confirmed:
      updates Order status → Confirmed

  → order.dispatched:
      updates Order status → Out for Delivery

  → order.delivered:
      calls confirm_deduction(vendor, product, qty, order_id)
      clears reserved_qty, updates physical_qty
      triggers loyalty earn

  → order.cancel (vendor-initiated):
      calls release_reservation(vendor, product, qty, order_id)
      cancels Order
      triggers refund if paid
      notifies customer
      suggests nearest alternative vendor
```

### Updated Hooks (hooks.py additions)
```python
# New scheduled tasks
scheduler_events = {
  "daily": [
    "saathimart.api.stock.alert_stale_vendor_stock",
    # alerts admin if vendor hasn't synced in > 24 hours
  ],
  "cron": {
    "*/5 * * * *": [
      "saathimart.api.stock.check_negative_vendor_stock",
      # catches any available_qty < 0 and alerts admin
    ],
  }
}

# New doc events
doc_events = {
  "Vendor Stock": {
    "on_update": "saathimart.api.stock.on_vendor_stock_update",
    # auto-updates Product.stock_qty = SUM of all vendor available_qty
    # so existing product list queries still work
  }
}
```

### Order Flow (updated end to end)
```
1. Customer opens app → shares GPS
2. App calls resolve_vendors(lat, lng) → gets nearby vendors
3. App calls list_products(lat, lng, vendor=nearest) → products with stock
4. Customer adds to cart → cart item carries vendor_id
5. Customer hits checkout:
     a. For each item: atomic_reserve(vendor, product, qty)
        → if fails: "Just sold out, try [next vendor]"
     b. Order created (status=Pending)
     c. Webhook Event queued: order.new → vendor ERPNext
6. Vendor ERPNext receives order.new → creates Sales Order
7. Vendor confirms → pushes order.confirmed → hub Order → Confirmed
8. Vendor dispatches → pushes order.dispatched → hub Order → Out for Delivery
9. Vendor delivers → pushes order.delivered → hub:
     → confirm_deduction clears reservation
     → loyalty points earned
     → payment settled (COD) or already paid (eSewa/Khalti)
```

---

## Repo 2 — `saathimart-vendor` (Vendor ERPNext App) — Full Spec

### What this app is

A standard Frappe app installed on the vendor's existing ERPNext site via:
```
bench get-app https://github.com/your-org/saathimart-vendor
bench --site vendor.local install-app saathimart_vendor
```

It does NOT replace ERPNext. It hooks into ERPNext's existing doctypes
(Item, Sales Order, Delivery Note, Purchase Receipt, Stock Ledger Entry)
and adds the sync layer on top.

### New Doctypes

#### SM Vendor Config (Single DocType — one per site)
```
Fields:
  hub_url           Data     (reqd) e.g. https://saathimart.com.np
  vendor_id         Data     (reqd) e.g. vendor-a  (assigned by hub admin)
  api_key           Data     (reqd) hub API key for this vendor
  api_secret        Password (reqd) hub API secret
  webhook_secret    Password        for verifying inbound hub webhooks
  default_warehouse Link → Warehouse
  sync_enabled      Check default 1
  last_sync_at      Datetime read_only
  hub_status        Data read_only  (Active/Suspended/Unreachable)

  section_reconciliation:
  reconciliation_enabled      Check default 1
  reconciliation_threshold    Float default 2  (units of drift before alert)
  reconciliation_threshold_pct Float default 5 (% drift before alert)
```

#### SM Product Mapping
```
Fields:
  barcode           Data    (reqd, in_list_view) ← physical barcode
  item_code         Link → Item (reqd, in_list_view) ← ERPNext item
  item_name         Data read_only fetch_from item_code.item_name
  hub_product_id    Data    (in_list_view) ← hub's SM-PROD-xxx name
  hub_sku           Data    ← hub's canonical SKU
  is_active         Check default 1
  last_synced       Datetime read_only
  sync_status       Select: Mapped/Unmapped/Error  default Unmapped

Actions on this doctype:
  "Sync with Hub" button → calls hub lookup_by_barcode → fills hub_product_id
  "Bulk Import" → CSV upload of item_code + barcode
```

#### SM Sync Outbox
```
Fields:
  event_type    Select: stock.receipt/stock.deduct/stock.adjustment/
                        order.confirmed/order.dispatched/order.delivered/
                        order.cancel
                (reqd, in_list_view)
  payload       Code (JSON, reqd)
  status        Select: Pending/Sent/Failed/Dead  default Pending (in_list_view)
  retry_count   Int default 0
  next_retry_at Datetime
  last_error    Small Text read_only
  voucher_type  Data  (which ERPNext doctype triggered this)
  voucher_no    Data  (ERPNext document name)
  created_at    Datetime read_only

Permissions: SM Vendor Admin rw
No delete permission (audit trail)
```

#### SM Vendor Order
```
Fields:
  hub_order_id      Data (reqd, unique, in_list_view) ← SM-ORD-xxx from hub
  sales_order       Link → Sales Order  ← ERPNext Sales Order created for this
  status            Select: Received/Accepted/Preparing/Dispatched/
                            Delivered/Cancelled  default Received (in_list_view)
  customer_name     Data
  customer_phone    Data
  delivery_address  Small Text
  delivery_lat      Float
  delivery_lng      Float
  grand_total       Currency
  payment_method    Data
  payment_status    Data
  items_json        Code (JSON) ← full items list from hub
  received_at       Datetime
  accepted_at       Datetime
  dispatched_at     Datetime
  delivered_at      Datetime
  notes             Small Text

Actions:
  "Accept Order" button → creates ERPNext Sales Order, pushes order.confirmed
  "Mark Dispatched"     → creates Delivery Note, pushes order.dispatched
  "Mark Delivered"      → submits Delivery Note, pushes order.delivered
  "Cancel Order"        → pushes order.cancel to hub with reason
```

### ERPNext Hooks

#### hooks.py
```python
app_name = "saathimart_vendor"

# Stock Ledger Entry is the single source of truth for ALL stock movements.
# Every stock transaction (Purchase Receipt, Sales Invoice, Stock Entry,
# Delivery Note, Stock Reconciliation, Manufacturing, Subcontracting, etc.)
# creates SLE records. Hooking into SLE catches everything.
doc_events = {
    "Stock Ledger Entry": {
        "on_submit": "saathimart_vendor.hooks.stock.on_stock_ledger_entry_submit",
        "on_cancel":  "saathimart_vendor.hooks.stock.on_stock_ledger_entry_cancel",
    },
    "Sales Order": {
        "on_submit": "saathimart_vendor.hooks.orders.on_sales_order_submit",
        "on_cancel":  "saathimart_vendor.hooks.orders.on_sales_order_cancel",
    },
    "Delivery Note": {
        "on_submit": "saathimart_vendor.hooks.orders.on_delivery_note_submit",
        "on_cancel":  "saathimart_vendor.hooks.orders.on_delivery_note_cancel",
    },
}

scheduler_events = {
    "cron": {
        "* * * * *": [
            # Every 1 min: flush Pending outbox entries to hub
            "saathimart_vendor.tasks.flush_outbox",
        ],
        "*/5 * * * *": [
            # Every 5 min: check hub health, alert if unreachable
            "saathimart_vendor.tasks.check_hub_health",
        ],
        "0 * * * *": [
            # Every hour: reconcile stock with hub
            "saathimart_vendor.tasks.reconcile_stock",
        ],
    }
}
```

### Hook Logic

#### `hooks/stock.py`
```python
def on_purchase_receipt_submit(doc, method):
    """Goods received → stock increases → push stock.receipt to hub."""
    for item in doc.items:
        mapping = get_mapping(item.item_code)
        if not mapping:
            log_unmapped(item.item_code, doc.name)
            continue
        enqueue_outbox(
            event_type="stock.receipt",
            payload={
                "barcode":     mapping.barcode,
                "hub_product": mapping.hub_product_id,
                "qty_change":  item.qty,
                "voucher_no":  doc.name,
                "vendor_id":   get_vendor_id(),
                "source_site": get_site_url(),
            },
            voucher_type="Purchase Receipt",
            voucher_no=doc.name,
        )

def on_purchase_receipt_cancel(doc, method):
    """Purchase receipt cancelled → reverse the stock.receipt."""
    for item in doc.items:
        mapping = get_mapping(item.item_code)
        if not mapping:
            continue
        enqueue_outbox(
            event_type="stock.deduct",
            payload={
                "barcode":     mapping.barcode,
                "hub_product": mapping.hub_product_id,
                "qty_change":  -item.qty,
                "voucher_no":  f"CANCEL-{doc.name}",
                "vendor_id":   get_vendor_id(),
                "source_site": get_site_url(),
                "remarks":     f"Purchase Receipt {doc.name} cancelled",
            },
            voucher_type="Purchase Receipt Cancel",
            voucher_no=doc.name,
        )

def on_sales_invoice_submit(doc, method):
    """Counter sale (NOT via SaathiMart) → stock decreases → push stock.deduct."""
    # Skip if this invoice was created from a SaathiMart SM Vendor Order
    # (those are handled by the order flow, not here)
    if _is_saathimart_order(doc):
        return
    for item in doc.items:
        mapping = get_mapping(item.item_code)
        if not mapping:
            log_unmapped(item.item_code, doc.name)
            continue
        enqueue_outbox(
            event_type="stock.deduct",
            payload={
                "barcode":     mapping.barcode,
                "hub_product": mapping.hub_product_id,
                "qty_change":  -item.qty,
                "voucher_no":  doc.name,
                "vendor_id":   get_vendor_id(),
                "source_site": get_site_url(),
                "remarks":     "Counter sale",
            },
            voucher_type="Sales Invoice",
            voucher_no=doc.name,
        )

def on_stock_entry_submit(doc, method):
    """Manual stock adjustment → push stock.adjustment to hub."""
    for item in doc.items:
        mapping = get_mapping(item.item_code)
        if not mapping:
            continue
        # Stock Entry can be receipt (qty positive) or issue (qty negative)
        qty_change = item.qty if doc.stock_entry_type == "Material Receipt" else -item.qty
        enqueue_outbox(
            event_type="stock.adjustment",
            payload={
                "barcode":     mapping.barcode,
                "hub_product": mapping.hub_product_id,
                "qty_change":  qty_change,
                "voucher_no":  doc.name,
                "vendor_id":   get_vendor_id(),
                "source_site": get_site_url(),
                "remarks":     f"Stock Entry: {doc.stock_entry_type}",
            },
            voucher_type="Stock Entry",
            voucher_no=doc.name,
        )
```

#### `hooks/orders.py`
```python
def on_sales_order_submit(doc, method):
    """Sales Order submitted → if linked to SM Vendor Order → push order.confirmed."""
    hub_order_id = frappe.db.get_value(
        "SM Vendor Order", {"sales_order": doc.name}, "hub_order_id"
    )
    if not hub_order_id:
        return  # not a SaathiMart order
    enqueue_outbox(
        event_type="order.confirmed",
        payload={"order_id": hub_order_id, "vendor_id": get_vendor_id(),
                 "sales_order": doc.name},
        voucher_type="Sales Order",
        voucher_no=doc.name,
    )

def on_delivery_note_submit(doc, method):
    """Delivery Note submitted → push order.dispatched to hub."""
    # Find linked SM Vendor Order via Sales Order
    so_name = doc.items[0].against_sales_order if doc.items else None
    if not so_name:
        return
    hub_order_id = frappe.db.get_value(
        "SM Vendor Order", {"sales_order": so_name}, "hub_order_id"
    )
    if not hub_order_id:
        return
    enqueue_outbox(
        event_type="order.dispatched",
        payload={"order_id": hub_order_id, "vendor_id": get_vendor_id(),
                 "delivery_note": doc.name},
        voucher_type="Delivery Note",
        voucher_no=doc.name,
    )
```

### Background Tasks

#### `tasks.py`
```python
def flush_outbox():
    """Every 1 min — push Pending SM Sync Outbox entries to hub."""
    config = get_config()
    if not config or not config.sync_enabled:
        return

    pending = frappe.get_list(
        "SM Sync Outbox",
        filters={"status": "Pending"},
        fields=["name", "event_type", "payload", "retry_count"],
        order_by="creation asc",
        limit=50,
    )
    for row in pending:
        _push_to_hub(config, row)

def _push_to_hub(config, row):
    import requests, json
    try:
        resp = requests.post(
            f"{config.hub_url}/api/method/saathimart.api.events.receive",
            json={"event": row.event_type, "payload": json.loads(row.payload)},
            headers={
                "X-SM-Secret": config.get_password("webhook_secret") or "",
                "X-Vendor-ID": config.vendor_id,
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        if resp.ok:
            frappe.db.set_value("SM Sync Outbox", row.name, "status", "Sent")
        else:
            _handle_outbox_failure(row, f"HTTP {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        _handle_outbox_failure(row, str(e)[:200])
    frappe.db.commit()

def _handle_outbox_failure(row, error):
    retry_count = (row.retry_count or 0) + 1
    status = "Dead" if retry_count > 10 else "Pending"
    import frappe.utils
    next_retry = frappe.utils.add_to_date(
        frappe.utils.now_datetime(), minutes=min(2 ** retry_count, 60)
    )
    frappe.db.set_value("SM Sync Outbox", row.name, {
        "status": status,
        "retry_count": retry_count,
        "next_retry_at": next_retry,
        "last_error": error,
    })
    if status == "Dead":
        frappe.sendmail(
            recipients=[frappe.db.get_single_value("SM Vendor Config", "admin_email") or ""],
            subject=f"SaathiMart Sync Failed: {row.event_type}",
            message=f"Outbox entry {row.name} failed after 10 retries.\nError: {error}",
        )

def check_hub_health():
    """Every 5 min — ping hub, update hub_status on SM Vendor Config."""
    import requests
    config = get_config()
    if not config:
        return
    try:
        resp = requests.get(f"{config.hub_url}/api/method/ping", timeout=5)
        status = "Active" if resp.ok else "Unreachable"
    except Exception:
        status = "Unreachable"
    frappe.db.set_value("SM Vendor Config", config.name, "hub_status", status)
    if status == "Unreachable":
        pending_count = frappe.db.count("SM Sync Outbox", {"status": "Pending"})
        frappe.log_error(
            f"Hub unreachable. {pending_count} events pending in outbox.",
            "SaathiMart Hub Health"
        )

def reconcile_stock():
    """Every hour — compare ERPNext actual stock vs hub VendorStock, push adjustments."""
    config = get_config()
    if not config or not config.reconciliation_enabled:
        return

    mappings = frappe.get_list(
        "SM Product Mapping",
        filters={"is_active": 1, "hub_product_id": ["!=", ""]},
        fields=["barcode", "item_code", "hub_product_id"],
    )
    for m in mappings:
        _reconcile_item(config, m)

def _reconcile_item(config, mapping):
    import requests
    # Get actual ERPNext stock
    actual_qty = frappe.db.get_value(
        "Bin",
        {"item_code": mapping.item_code,
         "warehouse": frappe.db.get_single_value("SM Vendor Config", "default_warehouse")},
        "actual_qty",
    ) or 0

    # Get hub's physical_qty for this vendor+product
    try:
        resp = requests.get(
            f"{config.hub_url}/api/method/saathimart.api.stock.get_vendor_stock",
            params={"vendor": config.vendor_id, "product": mapping.hub_product_id},
            headers={"X-SM-Secret": config.get_password("webhook_secret") or ""},
            timeout=10,
        )
        if not resp.ok:
            return
        hub_physical = resp.json().get("message", {}).get("physical_qty", 0)
    except Exception:
        return

    drift = actual_qty - hub_physical
    threshold = frappe.db.get_single_value("SM Vendor Config", "reconciliation_threshold") or 2
    threshold_pct = frappe.db.get_single_value(
        "SM Vendor Config", "reconciliation_threshold_pct"
    ) or 5

    pct_drift = abs(drift / hub_physical * 100) if hub_physical else 100
    if abs(drift) > threshold or pct_drift > threshold_pct:
        enqueue_outbox(
            event_type="stock.adjustment",
            payload={
                "barcode":     mapping.barcode,
                "hub_product": mapping.hub_product_id,
                "qty_change":  drift,
                "voucher_no":  f"RECON-{frappe.utils.nowdate()}",
                "vendor_id":   config.vendor_id,
                "source_site": frappe.utils.get_url(),
                "remarks":     f"Hourly reconciliation. ERPNext={actual_qty} Hub={hub_physical}",
            },
            voucher_type="Reconciliation",
            voucher_no=f"RECON-{frappe.utils.nowdate()}",
        )
```

---

### Inbound Webhook Handler (hub → vendor)

#### `api/receive.py` (on vendor ERPNext)
```python
@frappe.whitelist(allow_guest=True)
def receive_from_hub(event=None, payload=None):
    """Hub pushes events to vendor ERPNext here."""
    _verify_hub_secret()
    if not event:
        frappe.throw("event is required")

    payload = payload or {}

    if event == "order.new":
        _handle_new_order(payload)
    elif event == "order.cancel":
        _handle_order_cancel(payload)
    elif event == "order.reassign":
        _handle_order_reassign(payload)
    else:
        frappe.log_error(f"Unknown event from hub: {event}", "SM Vendor Receive")

    return {"ok": True}


def _handle_new_order(payload):
    """Hub sends new order → create SM Vendor Order record."""
    hub_order_id = payload.get("order_id")
    if frappe.db.exists("SM Vendor Order", {"hub_order_id": hub_order_id}):
        return  # idempotent — already received

    doc = frappe.new_doc("SM Vendor Order")
    doc.hub_order_id     = hub_order_id
    doc.status           = "Received"
    doc.customer_name    = payload.get("customer_name")
    doc.customer_phone   = payload.get("customer_phone")
    doc.delivery_address = payload.get("delivery_address")
    doc.delivery_lat     = payload.get("delivery_lat")
    doc.delivery_lng     = payload.get("delivery_lng")
    doc.grand_total      = payload.get("grand_total")
    doc.payment_method   = payload.get("payment_method")
    doc.payment_status   = payload.get("payment_status")
    doc.items_json       = frappe.as_json(payload.get("items", []))
    doc.received_at      = frappe.utils.now_datetime()
    doc.insert(ignore_permissions=True)
    frappe.db.commit()

    # Notify vendor staff via desk notification
    frappe.publish_realtime(
        "new_saathimart_order",
        {"order_id": hub_order_id, "total": payload.get("grand_total")},
        user="Administrator",
    )


def _handle_order_cancel(payload):
    """Hub cancels an order (customer cancelled or payment failed)."""
    hub_order_id = payload.get("order_id")
    vendor_order = frappe.db.get_value(
        "SM Vendor Order", {"hub_order_id": hub_order_id}, "name"
    )
    if not vendor_order:
        return
    frappe.db.set_value("SM Vendor Order", vendor_order, "status", "Cancelled")

    # If Sales Order was already created, cancel it
    so_name = frappe.db.get_value("SM Vendor Order", vendor_order, "sales_order")
    if so_name:
        so = frappe.get_doc("Sales Order", so_name)
        if so.docstatus == 1:
            so.cancel()
    frappe.db.commit()


def _handle_order_reassign(payload):
    """Hub reassigned this order away from this vendor (e.g. vendor was unreachable)."""
    hub_order_id = payload.get("order_id")
    vendor_order = frappe.db.get_value(
        "SM Vendor Order", {"hub_order_id": hub_order_id}, "name"
    )
    if vendor_order:
        frappe.db.set_value("SM Vendor Order", vendor_order, "status", "Cancelled")
        frappe.db.set_value(
            "SM Vendor Order", vendor_order, "notes",
            f"Reassigned by hub: {payload.get('reason', '')}"
        )
    frappe.db.commit()


def _verify_hub_secret():
    config = frappe.get_single("SM Vendor Config")
    secret = config.get_password("webhook_secret", raise_exception=False) or ""
    if not secret:
        return
    incoming = frappe.request.headers.get("X-SM-Secret", "")
    if incoming != secret:
        frappe.throw("Invalid secret", frappe.AuthenticationError)
```

---

## Implementation Phases

### Phase 1 — Hub: VendorStock + Location + Atomic Reserve
```
Files to create/update in saathimart (Repo 1):

CREATE:
  saathimart/doctype/vendor_stock/          (VendorStock doctype)
  saathimart/doctype/vendor_location/       (VendorLocation doctype)
  saathimart/doctype/product_barcode_mapping/ (barcode → product lookup)
  saathimart/api/location.py               (resolve_vendors, nearest_vendor_for_product)
  saathimart/api/stock.py                  (atomic_reserve, release, confirm_deduction,
                                            apply_vendor_stock_event, get_vendor_stock)

UPDATE:
  saathimart/doctype/cart_item/cart_item.json  → add vendor field
  saathimart/doctype/order/order.json          → add net_total, total_taxes,
                                                  total_discount fields (missing)
  saathimart/doctype/vendor/vendor.json        → add lat, lng, service_radius_km,
                                                  last_sync_at, hub_status fields
  saathimart/api/cart.py                       → vendor per item, soft stock check
  saathimart/api/orders.py                     → atomic reserve at checkout
  saathimart/api/products.py                   → location filter, lookup_by_barcode
  saathimart/api/events.py                     → barcode resolution, VendorStock update
  saathimart/hooks.py                          → new scheduler events
```

### Phase 2 — Vendor App: Scaffold + Config + Mapping
```
CREATE new repo: saathimart-vendor

saathimart_vendor/
  __init__.py
  hooks.py
  modules.txt
  patches.txt
  setup.py
  pyproject.toml

  doctype/
    vendor_config/     (Single doctype)
    product_mapping/   (barcode ↔ item_code ↔ hub_product_id)
    sync_outbox/       (transactional outbox)
    vendor_order/      (inbound orders from hub)

  hooks/
    __init__.py
    stock.py            (Stock Ledger Entry hooks — catches ALL stock movements)
    orders.py           (Sales Order, Delivery Note hooks)

  api/
    __init__.py
    receive.py            (inbound webhook from hub)
    mapping.py            (sync_with_hub, bulk_import, lookup_barcode, sync_vendor_stock, sync_vendor_location)

  tasks.py                (flush_outbox, check_hub_health, reconcile_stock)
  utils.py                (get_config, get_vendor_id, enqueue_outbox, get_mapping)
```

### Stock Ledger Entry — Single Hook for All Stock Movements

Instead of hooking into individual ERPNext doctypes (Purchase Receipt, Sales Invoice, Stock Entry, etc.),
the vendor app hooks into **Stock Ledger Entry** — the single source of truth for ALL stock movements
in ERPNext. Every stock transaction creates SLE records, so this approach catches everything:

- Purchase Receipt (goods received from supplier)
- Sales Invoice (counter sale or delivery)
- Stock Entry (material receipt, issue, transfer, manufacture, repack, subcontract)
- Delivery Note (delivery to customer or return from customer)
- Stock Reconciliation (manual stock adjustment)
- Manufacturing / Work Order (raw material consumption)
- Subcontracting (material issued to subcontractor)
- Asset Movement (stock moved between locations)
- Any other stock-moving document

The `on_stock_ledger_entry_submit` handler reads the SLE's `voucher_type` and `actual_qty`
to determine the stock movement direction and pushes the appropriate event to the hub.
The `on_stock_ledger_entry_cancel` handler reverses the movement when an SLE is cancelled.

### Phase 3 — Integration Testing
```
Test scenarios to verify:

1. Vendor maps item via barcode scan → hub returns product → mapping saved
2. Vendor receives Purchase Receipt → SLE created → outbox entry → hub VendorStock updated
3. Vendor submits Sales Invoice (counter sale) → SLE created → outbox entry → hub available_qty decreases
4. Vendor creates Stock Entry (Material Receipt) → SLE created → outbox entry → hub stock increases
5. Vendor creates Stock Entry (Material Issue) → SLE created → outbox entry → hub available_qty decreases
6. Vendor delivers via Delivery Note → SLE created → outbox entry → hub available_qty decreases
7. Customer checkout → atomic reserve succeeds → order pushed to vendor
8. Two customers checkout simultaneously → one wins, one gets "sold out"
9. Vendor site down → hub retries → vendor comes back → order received
10. Hub down → vendor sells → outbox holds → hub returns → outbox flushes
11. Both sell last unit → negative available_qty → conflict alert → admin resolves
12. Hourly reconciliation → drift detected → adjustment pushed → hub corrected
13. Vendor cancels order → hub releases reservation → customer notified
14. Stock Reconciliation (positive) → SLE → hub stock increases
15. Stock Reconciliation (negative) → SLE → hub stock decreases
16. Manufacturing order → SLE → hub stock decreases (raw material consumption)
17. Subcontracting → SLE → hub stock decreases
```

### Phase 4 — Location-Based Product Display
```
UPDATE saathimart frontend (or API consumers):

GET /api/method/saathimart.api.location.resolve_vendors
  ?lat=27.72&lng=85.32&radius_km=5
  → returns vendors sorted by distance

GET /api/method/saathimart.api.products.list_products
  ?lat=27.72&lng=85.32&radius_km=5
  → returns products from nearest vendor with stock
  → each product includes: vendor_name, distance_km, available_qty

Customer sees Blinkit-style:
  "Delivered in 20 min from Vendor A (0.3 km)"
  "Only 3 left"
```

---

## Error Reference Table

| Error | Where | Cause | Resolution |
|---|---|---|---|
| ROW_COUNT=0 on atomic reserve | Hub checkout | Someone else grabbed last unit | Show "sold out", suggest next vendor |
| available_qty < 0 after vendor push | Hub stock.py | Counter sale raced with ecommerce order | Stock Conflict alert → vendor confirms or cancels |
| Outbox entry Dead (>10 retries) | Vendor tasks.py | Hub unreachable for extended period | Admin email alert, manual re-queue |
| Webhook Event Dead (>5 retries) | Hub publisher.py | Vendor site unreachable | Admin alert, reassign or cancel order |
| Unmapped product barcode | Hub events.py | Vendor pushed unknown barcode | Hold event, alert admin, admin maps it, re-queue |
| Reconciliation drift > threshold | Vendor tasks.py | Lost events or manual adjustments | Auto-push adjustment SLE to hub |
| Hub status = Unreachable | Vendor tasks.py | Hub down | Vendor admin alerted, outbox accumulates |
| Sales Order cancel after dispatch | Vendor hooks | Vendor cancelled after Delivery Note | Hub order → Cancelled, refund triggered |
| Duplicate order.new received | Vendor receive.py | Hub retried after timeout | Idempotent check on hub_order_id, skip |
| Vendor suspended on hub | Hub auth | Admin suspended vendor | All vendor's products hidden from customers |

---

## Auth System

### Overview

SaathiMart implements a complete OTP-based auth system modeled after
trevo_ecommerce patterns but self-contained (no ERPNext dependency).

### Endpoints

| Method | Endpoint | Purpose | Auth Required |
|---|---|---|---|
| POST | `/api/method/saathimart.api.auth_full.signup` | Register new user with OTP | No |
| POST | `/api/method/saathimart.api.auth_full.verify_signup_otp` | Verify OTP, activate user | No |
| POST | `/api/method/saathimart.api.auth_full.login` | Login, returns bearer token | No |
| POST | `/api/method/saathimart.api.auth_full.forgot_password` | Send password reset OTP | No |
| POST | `/api/method/saathimart.api.auth_full.verify_forgot_password_otp` | Reset password with OTP | No |
| POST | `/api/method/saathimart.api.auth_full.resend_otp` | Resend OTP | No |
| POST | `/api/method/saathimart.api.auth_full.change_password` | Change password | Yes |
| GET | `/api/method/saathimart.api.auth.get_profile` | Get user profile | Yes |
| POST | `/api/method/saathimart.api.auth.update_profile` | Update name/phone | Yes |

### Token-Based Authentication

The frontend (saathimart-fe) authenticates using Frappe's built-in API key/secret:

1. **Login**: `POST /api/method/saathimart.api.auth_full.login` → returns `{api_key, api_secret, token}`
2. **Subsequent requests**: Include header `Authorization: token <api_key>:<api_secret>`
3. **Token generation**: `get_user_token()` creates an api_key/api_secret pair on the User doctype
4. **Token refresh**: Call login again to get a new pair; existing tokens remain valid

### Email Sending

| Endpoint | Email Sent |
|---|---|
| `signup` | OTP verification code |
| `forgot_password` | OTP for password reset |
| `resend_otp` | Resend OTP |
| `order.delivered` | Order confirmation (via `send_order_confirmation`) |

### Pending Verification Doctype

Stores OTP records for signup and password reset flows. Records expire after 15 minutes.

---

## Vendor Stock & Location Mapping

### Vendor Doctype (Updated)

The Vendor doctype now explicitly represents the person/entity who holds stock.

| Field | Type | Purpose |
|---|---|---|
| `lat` | Float | Vendor warehouse latitude |
| `lng` | Float | Vendor warehouse longitude |
| `service_radius_km` | Float | Delivery radius (default 5 km) |
| `default_warehouse` | Link → Warehouse | Vendor's ERPNext warehouse |
| `total_available_qty` | Float (read_only) | Sum of all VendorStock.available_qty |
| `total_physical_qty` | Float (read_only) | Sum of all VendorStock.physical_qty |
| `last_sync_at` | Datetime (read_only) | When vendor last pushed stock |
| `hub_status` | Select | Active/Unreachable/Suspended |
| `notes` | Small Text | Free-text notes |

### Vendor App Stock/Location Sync

The saathimart-vendor app now includes:

- **`sync_vendor_stock()`** — Pushes current ERPNext Bin stock for all mapped products to the hub. Called by warehouse staff after stock-taking.
- **`sync_vendor_location()`** — Pushes vendor location (lat/lng/radius) to the hub. Called when vendor updates delivery settings.
- **`hub_post()`** — Helper for outbound HTTP POST to hub API with auth headers.

### How Vendor = Stock Holder Works

1. Each Vendor row in the hub represents a physical person/entity who holds stock.
2. VendorStock rows are scoped to `(vendor, product)` — only that vendor's stock.
3. When a customer adds to cart, the `vendor` field on the cart item determines which vendor's stock is reserved.
4. The vendor app's `sync_vendor_stock()` pushes ERPNext Bin actual_qty to the hub's VendorStock.
5. The vendor app's `sync_vendor_location()` pushes the vendor's delivery location to the hub's VendorLocation.
6. If a vendor is suspended on the hub, all their products are hidden from customers.

---

## File Count Summary (Updated)

```
Repo 1 (saathimart) additions:
  2 new doctypes (Pending Verification, updated Vendor)
  3 new API files (auth_full.py, mailing.py, location.py)
  4 updated files (auth.py, events.py, hooks.py, vendor.json)
  1 updated vendor.py

Repo 2 (saathimart-vendor) additions:
  1 updated API file (mapping.py — added sync_vendor_stock, sync_vendor_location, hub_post)
  0 new doctypes (uses existing Vendor Config, Product Mapping, Sync Outbox, Vendor Order)

---

## Docker Deployment — 502 Bad Gateway Fixes

### Root Causes Found and Fixed

1. **`bench serve` binding to `127.0.0.1`** — Frappe's `bench serve` binds to `0.0.0.0` by default, but if the `gunicorn_bind` config is set to `127.0.0.1:8000`, nginx in a separate container can't reach it. Fixed by explicitly setting `bench set-config -g gunicorn_bind "0.0.0.0:8000"` in both init scripts.

2. **Vendor init.sh starting multiple `bench serve` processes** — The old vendor init.sh started one `bench serve` per vendor site on different ports (8001, 8002, 8003). This is incorrect because `bench serve` serves all sites on the bench on a single port. Fixed by using a single `bench serve --port 8000` process and routing all vendor sites through nginx by hostname.

3. **nginx starting before apps are healthy** — The nginx `depends_on` only waited for containers to start, not for the Frappe bench to be ready. Fixed by adding `condition: service_healthy` to nginx's `depends_on`.

4. **Missing Docker DNS resolver in nginx.conf** — nginx couldn't resolve container hostnames (`hub`, `vendors`) without a DNS resolver. Fixed by adding `resolver 127.0.0.11 valid=30s;` to nginx.conf.

5. **Vendor healthcheck checking wrong port** — The vendor healthcheck checked port 8001, but the vendor container now serves all sites on port 8000. Fixed by updating the healthcheck to check port 8000.

6. **Hub healthcheck start_period too short** — The hub healthcheck start_period was 120s, which is too short for initial bench setup. Increased to 180s.

### Key Docker Files

| File | Purpose |
|---|---|
| `saathimart/Dockerfile` | Hub container image |
| `saathimart/docker/init.sh` | Hub bench setup and serve |
| `saathimart-vendor/Dockerfile` | Vendor container image |
| `saathimart-vendor/docker/init.sh` | Vendor bench setup and serve |
| `nginx.conf` | Reverse proxy routing by hostname |
| `docker-compose.saathimart.yml` | Full stack orchestration |

### Running the Stack

```bash
# Start all services
docker compose -f docker-compose.saathimart.yml up -d

# Check status
docker compose -f docker-compose.saathimart.yml ps

# View logs
docker compose -f docker-compose.saathimart.yml logs -f hub
docker compose -f docker-compose.saathimart.yml logs -f vendors
docker compose -f docker-compose.saathimart.yml logs -f nginx

# Check nginx error logs (for 502 debugging)
docker exec sm-nginx cat /var/log/nginx/error.log
```

### Healthcheck Endpoints

- Hub: `http://localhost:8000/api/method/ping`
- Vendors: `http://localhost:8000/api/method/ping` (all vendor sites on same port)
