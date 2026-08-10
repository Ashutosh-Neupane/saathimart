# SaathiMart

Central commerce hub — Blinkit-style, Frappe-native, **no ERPNext**.

## Architecture

```
saathimart (central Frappe site)
├── Product / Category / Vendor
├── Order / Cart
├── Delivery Zone
├── REST API (whitelisted, guest-safe)
└── Redis Queue → cross-site event bus
        │
        ├── redis-cache  (Frappe page/query cache)
        └── redis-queue  (jobs + pub/sub, port 6380 exposed)
                │
        Vendor Site A ──GET /api/method/saathimart.api.events.poll
        Vendor Site B ──POST /api/method/saathimart.api.events.receive
```

## Pricing model

Products are priced per vendor, not per customer tier. The `Product Price`
child table on `Product` holds one row per vendor's "Site Price" (plus
optional hub-level `Retail` / zone rows as a fallback for products with no
vendor-specific price yet). Resolution order lives in
`saathimart.doctype.product.product.get_effective_price()`:

1. vendor + delivery zone match
2. vendor match (no zone)
3. hub-level zone match (no vendor)
4. hub-level price-type match (no vendor, no zone)
5. hub `Retail` row
6. `Product.price` base field

Vendor sites push price changes back to the hub via the `price.update` sync
event (`saathimart.api.events._apply_price_update`), which upserts the
matching `Product Price` row for that vendor.

## Key API endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `saathimart.api.products.list_products` | Product catalogue |
| GET | `saathimart.api.products.get_product` | Single product by slug |
| GET | `saathimart.api.products.list_categories` | Category tree |
| GET | `saathimart.api.cart.get_cart` | Get/create cart |
| POST | `saathimart.api.cart.add_to_cart` | Add item |
| POST | `saathimart.api.cart.update_cart_item` | Update qty |
| POST | `saathimart.api.orders.checkout` | Place order |
| GET | `saathimart.api.orders.get_order` | Order detail |
| POST | `saathimart.api.orders.update_order_status` | Admin/vendor status update |
| GET | `saathimart.api.events.poll` | Vendor site polls events |
| POST | `saathimart.api.events.receive` | Vendor site pushes back |

## Cross-site event flow

1. Order placed → `on_order_created` fires
2. Publishes to `saathimart:events` Redis channel (real-time)
3. Creates `SM Webhook Event` record (status=Queued)
4. Cron every 2 min: `drain_event_queue` POSTs to vendor `frappe_site_url`
5. Vendor site can also poll via `events.poll` with `?since=<datetime>`

## Quick start

```bash
cp .env.example .env
docker compose up --build
```

Site available at `http://localhost:8000`
Redis queue exposed at `localhost:6380` for vendor sites.

## Testing

The whole stack (hub + vendor sites) runs from the repo root:

```bash
docker compose -f docker-compose.saathimart.yml up --build -d
docker compose -f docker-compose.saathimart.yml exec hub \
    bench --site saathimart.localhost run-tests --app saathimart
```

Test suite lives in `saathimart/tests/test_saathimart.py` and covers product
pricing (hub tiers + per-vendor Site Price resolution), cart, checkout
totals, coupons, loyalty, eSewa signature verification, order status
transitions, stock ledger, and auth guards.
