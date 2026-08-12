# SaathiMart

Central commerce hub for a Blinkit-style multi-vendor marketplace — **plain
Frappe, no ERPNext dependency**. This repo is the customer-facing side: it
owns the product catalogue, cart/checkout, orders, payments, loyalty, and
the vendor registry. Each vendor runs their own ERPNext bench, kept in sync
through the sibling [`saathimart-vendor`](https://github.com/Ashutosh-Neupane/saathimart-vendor)
app.

## Contents

- [What this is](#what-this-is)
- [Architecture](#architecture)
- [How hub ↔ vendor sync works](#how-hub--vendor-sync-works)
- [Pricing model](#pricing-model)
- [Key API endpoints](#key-api-endpoints)
- [Quick start](#quick-start)
- [Testing](#testing)
- [Reports & dashboard](#reports--dashboard)
- [Payments](#payments)
- [Security](#security)
- [File reference](#file-reference)
- [Known limitations](#known-limitations)
- [Common pitfalls](#common-pitfalls)

## What this is

| | |
|---|---|
| Catalog | `Product`, `Product Media`, `Product Price`, `Product Specification`, `Category` |
| Cart & checkout | `Cart`, `Cart Item`, `Order`, `Order Item`, `Order Tax`, `Coupon` |
| Customer | `Address`, `Review`, `Wishlist`, `Loyalty Program`, `Loyalty Tier`, `Loyalty Point Entry` |
| Vendor (hub view) | `Vendor`, `Vendor Listing`, `Vendor Fulfillment`, `Vendor Payout`, `Vendor Stock`, `Vendor Barcode Index`, `Stock Ledger Entry` |
| Delivery | `Delivery Zone` (delivery charge, loyalty multiplier, first/second-order onboarding discount — all per zone) |
| CMS / content | `Site Config`, `Site Page`, `Homepage Settings`, `Hero Slide`, `Seasonal Banner`, `Banner`, `Trust Badge`, `Navigation Item`, `Product Rail Heading`, `Blog Post` |
| Other | `Settings`, `Webhook Event`, `SM Notification`, `Payment Log` |

The vendor side (a separate repo, `saathimart-vendor`) is an ERPNext app
installed on each vendor's own bench. It never touches the product
catalogue — it only owns `Product Mapping` (barcode ↔ ERPNext Item ↔ hub
Product), `Sync Outbox`, and `Vendor Order`.

## Architecture

```
                    ┌───────────────────────────┐
                    │     NGINX (routes by       │
                    │     Host header)            │
                    └─────────────┬───────────────┘
                                  │
              ┌───────────────────┴───────────────────┐
              │                                        │
    ┌─────────▼─────────┐                  ┌───────────▼──────────┐
    │  Next.js storefront │                  │   this repo (hub)     │
    │  (separate repo)     │                  │   saathimart.localhost │
    └─────────────────────┘                  │                        │
                                              │  Product / Order /     │
                                              │  Vendor Listing /      │
                                              │  Vendor Stock / ...    │
                                              │                        │
                                              │  events/publisher.py   │
                                              │  → Webhook Event queue │
                                              └───────────┬────────────┘
                                                           │ HTTPS webhooks
                                                           │ (HMAC signed)
                              ┌─────────────────────────────┼─────────────────────────────┐
                              │                              │                              │
                    ┌─────────▼──────────┐        ┌─────────▼──────────┐        ┌─────────▼──────────┐
                    │  Vendor 1 (ERPNext)  │        │  Vendor 2 (ERPNext)  │        │  Vendor N (ERPNext)  │
                    │  saathimart-vendor    │        │  saathimart-vendor    │        │  saathimart-vendor    │
                    │  Product Mapping      │        │  ...                  │        │  ...                  │
                    │  Sync Outbox          │        │                       │        │                       │
                    │  Vendor Order         │        │                       │        │                       │
                    └───────────────────────┘        └───────────────────────┘        └───────────────────────┘
```

Each vendor is its own independent Frappe/ERPNext installation with its own
database, Redis, and background workers — not a tenant on shared
infrastructure. The hub never reaches into a vendor's Redis or database;
everything crosses the boundary as an authenticated HTTPS webhook, the same
trust model Stripe/GitHub/Shopify use for third-party integrations. A
Redis-Streams / shared-queue design was evaluated and deliberately rejected
for exactly this reason.

### Data ownership

| Data | Owner | Sync direction |
|---|---|---|
| Product catalogue (name, description, images, specs) | Hub | Hub-only — desk UI, no public write API. Vendors are never notified of catalogue changes; they only find products via barcode lookup. |
| Vendor's price | Hub `Vendor Listing.price` | Vendor's ERPNext `Item Price` change → auto-pushes `price.update` → hub upserts |
| Stock | Hub `Vendor Stock` (authoritative) | Vendor → Hub automatically on every ERPNext stock movement; Hub also deducts at checkout and confirms on delivery |
| Barcode | Vendor `Product Mapping.barcode` + Hub `Vendor Listing.sku` | The **only universal shared key** across the two independently-owned systems |
| ERPNext Item Code | Vendor `Product Mapping.item_code` | Private to the vendor; never sent to the hub |
| Orders | Hub `Order` / `Vendor Fulfillment` | Hub creates; status flows both ways via webhooks |

## How hub ↔ vendor sync works

### Product mapping (how a vendor connects their inventory to a hub product)

There's no "push my product to the hub" flow — the hub is the sole source
of truth for the catalogue, and vendors are autonomous sellers who opt in.
Mapping is barcode-driven:

1. Vendor creates a `Product Mapping` row: `barcode` (physical barcode) +
   `item_code` (their ERPNext Item). Starts `Unmapped`.
2. Vendor triggers a sync — one mapping (`sync_with_hub()`), a CSV bulk
   import, or a retry-all-unmapped sweep.
3. Hub resolves the barcode (`lookup_by_barcode`) against `Vendor
   Listing.sku`, falling back to `Product.sku` for legacy data.
4. On match: `hub_product_id`/`hub_sku` are filled in, `sync_status` →
   `Mapped`, and the vendor's *current* price and stock are backfilled
   into a new `Vendor Listing`/`Vendor Stock` immediately — not left at
   zero until an unrelated future edit touches the item again.

Once mapped, the vendor can push stock, receive orders, and push price
changes for that product.

### Outbound notification, without broadcasting

When a brand-new hub Product is created, the hub doesn't blast it to every
vendor site. `Vendor Barcode Index` tracks which vendors have ever
registered a given barcode (pushed automatically whenever a vendor adds/
removes an ERPNext Item Barcode), so a new product only notifies the small
number of vendors who actually carry that physical item.

### Transport: transactional outbox, not a message broker

Both directions use the same pattern — a durable DB row is written in the
same transaction as the triggering change, then delivery is scheduled
**immediately** as a background job (`enqueue_after_commit=True` +
`deduplicate=True` + a stable `job_id`), with a periodic cron
(`drain_event_queue` on the hub, `flush_outbox` on the vendor) as the
fallback sweep for anything the instant path missed. Typical delivery
latency is roughly a second, not "wait for the next cron tick."

- **Hub → Vendor**: `Webhook Event` — order dispatch (`order.new`),
  cancellations, reassignment.
- **Vendor → Hub**: `Sync Outbox` — stock movements, price changes,
  barcode register/unregister, order status.
- Both carry `event_id` (idempotency — a replayed push is a no-op) and
  `event_seq` (a per-vendor monotonic sequence — an out-of-order push is
  rejected, not silently applied). Inbound webhook processing on the hub
  (`events.receive`/`bulk_receive`) authenticates and records the event,
  then hands the actual apply logic to a background job — it never runs
  inline on the web workers that also serve customer traffic.
- Stock events additionally carry `base_qty`, a staleness guard: if the
  vendor's reported total doesn't match what the hub currently has on
  record, the push is rejected rather than silently corrupting the ledger.

### Order lifecycle

| Vendor action | ERPNext side-effect | Event pushed | Hub effect |
|---|---|---|---|
| Accept | Creates + submits Sales Order | `order.confirmed` | Fulfillment → Confirmed |
| Dispatch | Creates + submits a real Delivery Note against the Sales Order (not just a status flip — the stock movement is accounted for in ERPNext immediately) | `order.dispatched` | Fulfillment → Dispatched |
| Deliver | Status flip | `order.delivered` | Confirms stock deduction, fulfillment → Delivered, loyalty points earned |
| Cancel | Cancels Sales Order | `order.cancel` | Releases stock reservation, fulfillment → Cancelled |

On a multi-vendor order, each vendor's `Vendor Fulfillment` row advances
independently — one vendor finishing first never advances the whole order
or touches another vendor's stock/reservation.

### Reconciliation

An hourly job compares each vendor's real ERPNext `Bin.actual_qty` against
what the hub has on record and pushes a `stock.adjustment` event if drift
exceeds a configurable threshold — a safety net under the event-driven
sync, not the primary path.

## Pricing model

Products are priced per vendor, not per customer tier. The `Product Price`
child table on `Product` holds one row per vendor's "Site Price" (plus
optional hub-level `Retail`/zone rows as a fallback). Resolution order
(`Product.get_effective_price()`):

1. vendor + delivery zone match
2. vendor match (no zone)
3. hub-level zone match (no vendor)
4. hub-level price-type match (no vendor, no zone)
5. hub `Retail` row
6. `Product.price` base field

### Location-based loyalty & onboarding discounts

Delivery Zone carries its own rates, so the same customer can earn or save
differently depending on where an order ships:

- `loyalty_multiplier` — applied on top of the customer's tier multiplier
  when earning points on delivery to this zone.
- `first_order_discount_pct` / `second_order_discount_pct` — auto-applied,
  no coupon code, based on the customer's real order count (any status —
  cancelling and re-ordering doesn't reset eligibility), capped by
  `onboarding_max_discount_amount`.

## Key API endpoints

| Method | Endpoint | Description |
|--------|----------|--------------|
| GET | `saathimart.api.products.list_products` | Product catalogue, filters + sort |
| GET | `saathimart.api.products.get_product` | Single product by slug |
| GET | `saathimart.api.products.get_vendor_listings` | Every vendor/branch offering a product, with live stock |
| GET | `saathimart.api.cart.get_cart` | Get/create cart |
| POST | `saathimart.api.cart.add_to_cart` | Add item — rejects if out of stock at that vendor and backorder isn't allowed |
| POST | `saathimart.api.cart.update_cart_item` | Update qty |
| POST | `saathimart.api.orders.checkout` | Place order — reserves vendor stock atomically |
| GET | `saathimart.api.orders.get_order` | Order detail |
| POST | `saathimart.api.orders.update_order_status` | Admin/vendor status update |
| GET | `saathimart.api.totals.preview_order_totals` | Live totals preview (coupon/onboarding/loyalty) before checkout |
| GET | `saathimart.api.loyalty.get_loyalty_balance` | Customer's points balance + tier |
| POST | `saathimart.api.events.receive` | Vendor pushes a single event |
| POST | `saathimart.api.events.bulk_receive` | Vendor pushes a batch of events |
| GET | `saathimart.api.events.poll` | Vendor catch-up polling since a given `event_seq` |

## Quick start

```bash
cp .env.example .env
docker compose up --build
```

Site available at `http://localhost:8000`. `REDIS_QUEUE_PORT` (default
`6380`) is exposed for local vendor-site testing against this hub.

To run the **full integrated stack** (hub + 3 vendor ERPNext sites + nginx
routing by `Host` header), use the outer `docker-compose.saathimart.yml` one
level up, which builds this repo and `saathimart-vendor` together:

```bash
docker compose -f ../docker-compose.saathimart.yml up -d --build
```

- Hub: `http://saathimart.localhost`
- Vendor sites: `http://vendor1.localhost`, `vendor2.localhost`, `vendor3.localhost`

## Testing

```bash
docker compose -f ../docker-compose.saathimart.yml exec hub \
    bench --site saathimart.localhost run-tests --app saathimart
```

`saathimart/tests/test_saathimart.py` — 150+ tests covering product
pricing (hub tiers + per-vendor Site Price resolution), cart, checkout
totals, coupons, location-based loyalty/onboarding discounts, eSewa
signature verification, order status transitions, stock reservation, and
the inbound webhook handlers' idempotency/ordering/staleness guards.

The suite is a plain `unittest.TestCase` (not `FrappeTestCase`), so it
doesn't get automatic per-test rollback — `setUpModule`/`tearDownModule`
snapshot and restore the `Settings` singleton around the run instead, so a
live site's real config survives a test run intact.

## Reports & dashboard

| Report | What it shows |
|---|---|
| Vendor Performance | Orders, revenue, fulfillment rate, avg order value per vendor |
| Vendor Stock Report | Current stock levels, low-stock / out-of-stock alerts |
| Delivery Zone Performance | Orders, revenue, delivery charges by zone |
| Coupon Usage | Times used, order value, discount impact per coupon |
| Payment Status | Revenue breakdown by payment method |
| Vendor Commission Reconciliation | Settled vs pending sales, commission, payout due per vendor |

**SaathiMart Command Center** dashboard — number cards (Pending Orders,
Active Vendors, Low Stock Alerts) and charts (Revenue by Zone, Orders by
Vendor, Order Status Breakdown).

## Payments

- eSewa v2, HMAC-SHA256 signature verification.
- Success/failure callbacks redirect to the storefront; a cron
  (`verify_esewa_status`, every 10 min) catches any lost callback.
- Sandbox/live toggle via `Settings`.
- `Payment Log` audit row on every transaction.

Khalti support was removed rather than left as a never-verified code path —
it existed but had no coverage against real sandbox credentials.

## Security

- All vendor→hub calls carry `X-SM-Secret` + `X-SM-Timestamp` (replay
  window) headers, verified against `Settings.webhook_secret` or the
  calling vendor's own `Vendor.webhook_secret` when set.
- New vendors self-register on first sync as `status = "Pending"` — an
  admin must approve before the vendor is customer-visible.
- Every order status transition (admin override or vendor-reported)
  creates an `SM Notification` for the customer.

## File reference

| File | Purpose |
|---|---|
| `api/events.py` | Inbound event receiver, `receive`/`bulk_receive`/`poll`, all `_apply_*` handlers |
| `api/stock.py` | Vendor Stock CRUD, atomic checkout reservation, reconciliation |
| `api/orders.py` | Order creation, checkout, vendor splitting, status transitions |
| `api/products.py` | Product/catalogue reads, barcode lookup, vendor listing creation |
| `api/cart.py` | Session-based cart, server-side stock guard on add-to-cart |
| `api/totals.py` | Tax/coupon/onboarding-discount/loyalty totals engine |
| `api/loyalty.py` | Points engine: earn (tier × zone multiplier), redeem, tier resolution |
| `api/payments.py` | eSewa integration |
| `api/archival.py` | Scheduled cleanup of old Webhook Event/Cart/Order rows |
| `events/publisher.py` | Outbound event publisher, `Webhook Event` queue, instant-delivery scheduling |
| `hooks.py` | DocType permissions, doc_events, scheduler_events |
| `tests/test_saathimart.py` | Hub test suite |

## Known limitations

- No real-time stock push to the frontend (polling only today).
- No product variants (size/color/etc.).
- No multi-warehouse support per vendor.
- Single-currency (NPR), no localization.
- No delivery-partner tracking app.

## Common pitfalls

Things that have bitten this codebase before — worth knowing before you
touch the sync layer:

1. **Forgetting `enqueue_after_commit=True`** — without it, a background
   job can run before the triggering transaction commits, and a worker on
   a different DB connection won't see the row it's supposed to process
   yet.
2. **Forgetting `deduplicate=True` + a stable `job_id`** — without it, the
   instant-delivery path and the cron fallback sweep can both deliver the
   same event.
3. **Computing `event_seq` as a plain read-then-write** — always update it
   atomically (`UPDATE ... SET seq = COALESCE(seq,0)+1`), never
   `SELECT` then `SET` in Python, or two concurrent events can land on the
   same sequence number.
4. **Skipping the `base_qty` staleness guard** on stock events — without
   it, a vendor pushing from stale local state can silently corrupt the
   hub's stock ledger.
5. **Letting `frappe.enqueue()`'s `QueueOverloaded` propagate** — wrap
   background-job scheduling in `safe_enqueue()` so a full queue degrades
   to "delivered by the next cron sweep" instead of failing the caller's
   request.
6. **Hardcoding a vendor's URL** — always resolve it from
   `Vendor.frappe_site_url`; vendors register themselves.
7. **Exposing the hub's Redis to vendor sites** — never. Vendor sites are
   separate businesses on separate infrastructure; cross the boundary with
   signed HTTPS webhooks only, the same trust model any multi-tenant SaaS
   uses for third-party webhooks.
8. **Raw SQL aggregate strings in query-builder `fields`** (e.g.
   `"MIN(price) as price"`) — this Frappe version rejects them; use dict
   syntax (`{"MIN": "price", "as": "price"}`) instead.

## License

See [LICENSE](LICENSE).
