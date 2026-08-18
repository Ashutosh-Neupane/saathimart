# SaathiMart Tutorial — Franchises, Products, and How They Connect

This walks through the actual mechanics of `saathi_middleware`: how a
franchise gets onboarded, how its products end up in the catalog, how to
create a product by hand, and how an order flows from cart to a delivered
item. Written from the real code paths, not the theory — file/function
names below are exactly where to look if something needs changing.

## 1. The mental model

There are two kinds of Frappe sites in this system:

- **`saathi_middleware`** (this app) — the hub. It has no inventory of its
  own. It's the customer-facing catalog, cart, checkout, and order/payment
  brain. Every franchise's products live here as `Saathi Item` records.
- **`saathi_ecommerce`** (a separate Frappe/ERPNext site per franchise) —
  the franchise's own store backend, running standard ERPNext `Item` /
  `Bin` (stock) doctypes. A franchise owner manages their real inventory
  here, exactly like any ERPNext store.

A **Franchise** record in `saathi_middleware` is the hub's registration of
one of these franchise sites — its location, delivery radius, delivery
pricing, and the API credentials that franchise's `saathi_ecommerce`
instance uses to push its items in.

There is no cross-franchise product identity. A "product" in the catalog
*is* one franchise's listing (`Saathi Item`), named
`"<franchise_site_code>-<item_code>"` — e.g. `SM-DEMO-BEV-001`. Two
franchises selling the same physical thing are two separate `Saathi Item`
docs; there's no shared "master product."

## 2. Registering a franchise

A `Franchise` doc (`saathi_middleware/saathi_middleware/doctype/franchise/franchise.json`)
holds:

| Field | Purpose |
|---|---|
| `site_code` | The franchise's unique short code (e.g. `SM-DEMO`) — this is also the doc's `name` and the prefix on every one of its items' docnames. |
| `franchise_name`, `city`, `address_line` | Display info. |
| `latitude`, `longitude`, `serviceable_radius_km` | Used for "is this address deliverable" checks (haversine distance) in `api/order.py::_get_serviceable_franchise` and `api/delivery.py::calculate_delivery_charge`. |
| `delivery_base_charge`, `free_delivery_upto_km`, `delivery_per_km_rate` | Delivery-fee formula for this franchise. |
| `api_key`, `api_secret` | Credentials the franchise's `saathi_ecommerce` site authenticates with when pushing item syncs (see §3). Generate these however you like (`frappe.generate_hash`) — there's no auto-provisioning UI yet. |
| `status` | Must be `Active` for the franchise to be sellable — `checkout()`, `calculate_delivery_charge`, and the catalog's `list_products` all filter on `franchise.status = 'Active'`. |

Create one from the desk (`/app/franchise/new`) or via a patch — see
`saathi_middleware/patches/seed_demo_catalog.py` for the exact shape used
to seed the demo franchise `SM-DEMO`.

## 3. How a franchise's products get into the catalog (the real sync path)

This is the flow a real franchise uses day to day — nothing manual.

```
saathi_ecommerce (Item created/updated/stock changed)
        │  doc_events hook (hooks.py) fires
        ▼
saathi_ecommerce/api/middleware_client.py
  queue_item_sync() / queue_bin_sync()
        │  frappe.enqueue(..., enqueue_after_commit=True)
        ▼
  sync_item_job(item_code, action)
        │  builds payload, writes a "Saathi Sync Log" row (status=Pending)
        ▼
  _attempt_push()  →  POST {middleware_url}/api/method/saathi_middleware.api.item.sync_item
        │  headers: X-Saathi-Api-Key / X-Saathi-Api-Secret (Franchise.api_key/api_secret)
        ▼
saathi_middleware/api/item.py :: sync_item()
  get_authenticated_franchise()  — looks up Franchise by api_key, verifies api_secret
        ▼
  _upsert_item(franchise, payload)
    docname = f"{franchise.name}-{item_code}"
    creates or updates that Saathi Item
```

**On the franchise side** (`saathi_ecommerce`), you just use ERPNext
normally:

1. Create/edit an `Item` — set `is_web_item = 1` for it to be eligible for
   sync at all (`queue_item_sync` skips anything that isn't a web item and
   has never synced before).
2. Give it a selling price via `Item Price` (against the price list in
   `Saathi Ecommerce Settings.price_list`) — falls back to
   `Item.standard_rate` if there's no `Item Price` row.
3. Stock comes from the `Bin` for `Saathi Ecommerce Settings.default_warehouse`
   — a stock reconciliation / delivery note / stock entry against that
   warehouse triggers `queue_bin_sync()` automatically.
4. That's it. The `doc_events` hooks enqueue a background job; within
   moments the item exists (or updates) in `saathi_middleware` as
   `Saathi Item` named `{site_code}-{item_code}`.

**What actually gets copied** (`build_item_payload` in
`saathi_ecommerce/api/middleware_client.py`): `item_code`, `item_name`,
`description`, `item_group`, `uom`, `price`, `stock_qty`, `barcode`,
`is_active` (`not disabled and is_web_item`), `pushed_at` (a timestamp
used for stale-write protection — `_upsert_item` ignores an incoming
payload whose `pushed_at` is older than what's already stored, so a
delayed retry can never clobber a newer update).

**If a sync fails** (middleware unreachable, etc.), the `Saathi Sync Log`
row stays `Failed`/`Pending` and gets retried by
`saathi_ecommerce`'s own `retry_failed_syncs()` cron. Check
`Saathi Sync Log` list view on the franchise site to debug a missing/stale
item — it has the exact payload, the HTTP response, and the error.

**To force a resync** without waiting for the next Item edit, from the
franchise site's desk console or a script:

```python
from saathi_ecommerce.api.middleware_client import sync_single_item, push_all_items
sync_single_item("ITEM-CODE")   # one item, immediate
push_all_items()                 # every enabled web item, queued
```

**Things that are *not* synced** and only exist on the middleware side:
`category` (a `Saathi Item Category` link — franchise items sync with
whatever `item_group` ERPNext has, but the storefront's category/slug
system is a `saathi_middleware`-only concept, see §5), `image` /
`gallery_images`, `avg_rating` / `review_count` (denormalized from
`SM Product Review`), `meta_title` / `meta_description`. Set those
directly on the `Saathi Item` doc in `saathi_middleware` after it's synced
in — a resync from the franchise side won't overwrite them (`_upsert_item`
only touches the fields listed in `build_item_payload`).

## 4. Creating a product by hand (no franchise sync)

For a demo/seed franchise (no real `saathi_ecommerce` instance behind
it — e.g. `SM-DEMO`), or to add middleware-only fields to a synced item,
create/edit a `Saathi Item` directly:

- Desk: `/app/saathi-item/new`
- Or a patch — the cleanest reference is
  `saathi_middleware/patches/seed_demo_catalog.py`, which seeds 30 real
  items across 9 categories for franchise `SM-DEMO`.

Minimum fields:

| Field | Notes |
|---|---|
| `franchise` | Link to an existing, `Active` `Franchise`. |
| `item_code` | Unique **within that franchise** — the doc's actual name becomes `{franchise}-{item_code}` (`autoname` in `saathi_item.json`), not `item_code` alone. |
| `item_name`, `description`, `price`, `stock_qty` | Self-explanatory. `stock_qty = None` means untracked/unlimited inventory; a number is a hard cap enforced by `cart.py::_check_stock` at add-to-cart and again at checkout. |
| `category` | Link to a `Saathi Item Category` (see §5) — optional but the storefront's category rails/filters key off it. |
| `is_active` | Must be `1` to be purchasable; `add_to_cart`/`checkout` both re-check this live. |
| `image` | A Frappe file URL (`/files/...`). No image = the frontend shows `/placeholder-product.svg`. |
| `gallery_images` | Child table of `Saathi Item Image` rows if you want more than one photo. |

**One important naming gotcha** (this bit an actual bug this session):
the storefront's catalog API (`api/catalog.py`) returns a product's
`"slug"` as the *full* docname (`"SM-DEMO-CH-004"`), and the cart/order
APIs (`api/cart.py::compose_product_name`) expect a **bare** `item_code`
plus a separate `franchise` param to reconstruct that same name. If you're
calling `add_to_cart`/`checkout` directly (not through the storefront
frontend, which already handles this), pass the bare `item_code` — passing
the full slug *and* a franchise together used to silently double-prefix
into a name nothing matches. `compose_product_name` now detects an
already-prefixed `item_code` and leaves it alone either way, but it's
worth knowing which shape you're passing.

## 5. Categories

`Saathi Item Category` (`saathi_middleware/saathi_middleware/doctype/saathi_item_category/`)
is just `category_name` + `image`. The frontend never sees the raw doc
name — everywhere (category filters, product rails, nav) it works off a
**slug** derived by `api/catalog.py::_slugify`: lowercase, strip
punctuation, collapse to hyphens. `"Dairy Bakery"` → `dairy-bakery`.
`"Dairy And Eggs"` → `dairy-and-eggs` — note this would **not** match a
category literally named `"Dairy & Eggs"` (which also slugifies to
`dairy-eggs`, missing the `and`). The exact wording of `category_name` is
load-bearing wherever the frontend hardcodes a category slug (home page
product rails, header/footer nav seeds) — see the comment at the top of
`seed_demo_catalog.py` for the full list of frontend call sites that key
off specific slugs.

## 6. How an order actually flows

```
Cart (SM Cart / SM Cart Item, keyed by a session_id cookie)
   │  api/cart.py: add_to_cart / update_cart_item / get_cart_summary
   ▼
Checkout — api/order.py :: checkout()
   │  resolves franchise from cart_doc.items[0].franchise (single-franchise
   │  cart — see below), re-validates stock/price/is_active live,
   │  applies coupon (Saathi Coupon) + loyalty redemption, creates a
   │  Saathi Order + Saathi Order Item rows, sets cart.status = "CheckedOut"
   ▼
Payment
   │  COD/offline: order confirmation email fires immediately, order synced
   │  to the franchise's ERPNext as a Sales Order (push_order_job, enqueued)
   │  eSewa/online: initiate_payment() signs a form POST to eSewa; eSewa's
   │  success/failure callback (api/payments.py) marks payment_status,
   │  bumps order.status, and only *then* fires the same confirmation
   │  email/notification/ERPNext-sync as the COD path
   ▼
push_order_job → syncs the placed order to the franchise's saathi_ecommerce
  as a real Sales Order against their ERPNext, so franchise staff see it
  in their own system
```

**A cart is single-franchise.** `checkout()` derives the whole order's
franchise from the *first* cart line's `franchise` field — there's no
mixed-franchise checkout. This is also why the delivery address isn't a
free choice at checkout time in the storefront: the delivery zone/charge
is the cart's franchise's own numbers (`Franchise.delivery_base_charge`
etc.), not something to pick independently.

**Cart identity**: `SM Cart.session_id` is unique. `checkout()` never
deletes the cart it just placed an order from (kept for order history) —
it flips `status` to `"CheckedOut"`. Frontend cart-session cookies are
long-lived (30 days), so `api/cart.py::_get_or_create_cart` defensively
frees up a stale non-`Active` cart's `session_id` before creating a new
one, and the frontend also rotates its cart-session cookie right after a
successful `placeOrder()` — belt and suspenders against the same
`session_id` colliding with a cart that's done being active.

## 7. Payment (eSewa)

Only eSewa is implemented as a real online gateway
(`api/payments.py`) — everything else (`Saathi Payment Mode.is_online = 0`)
just records the order as unpaid-until-delivery (COD-style). To go live:

1. `Settings.esewa_merchant_code` / `esewa_secret_key` — your real eSewa
   merchant credentials (Settings is a single doctype, `/app/settings`).
2. `Settings.payment_sandbox_mode` — leave `1` until you're actually ready
   for real money; it picks eSewa's sandbox host
   (`rc-epay.esewa.com.np`) vs production (`epay.esewa.com.np`).
3. `Settings.payment_portal_base_url` — your real storefront domain, so
   eSewa's success/failure redirect lands the shopper back on your actual
   site instead of this site's own URL.

`initiate_payment(order)` signs a fresh, uniquely-suffixed
`transaction_uuid` (`{order_name}-{unix_ts}`) per attempt — eSewa rejects
a reused `transaction_uuid` outright, which matters if a shopper retries
after a cancelled/failed payment for the same order.

## 8. Cron / background jobs to know about

Registered in `hooks.py::scheduler_events` — **the site-wide Frappe
scheduler has to actually be enabled** (`bench --site <site> scheduler
enable && bench --site <site> scheduler resume`; `bench doctor` shows
current status) or none of these ever run:

- `poll_pending_esewa_orders` (every 10 min) — catches an eSewa payment
  whose success/failure redirect never reached us.
- `expire_abandoned_carts` (hourly) — per `Settings.abandoned_cart_hours`.
- `cleanup_expired_verifications` (daily) — stale signup/reset OTPs.
- `expire_old_points` (daily) — loyalty point expiry per
  `SM Loyalty Program.point_expiry_days`.
- `retry_failed_order_syncs` (every 5 min, `api/order.py`) — retries
  `push_order_job` failures (the middleware→franchise ERPNext sync).

## 9. Quick reference — where things live

| Concept | Doctype | Key API module |
|---|---|---|
| Franchise registration | `Franchise` | — |
| A product listing | `Saathi Item` | `api/item.py` (sync), `api/catalog.py` (storefront read) |
| Category | `Saathi Item Category` | `api/catalog.py` |
| Cart | `SM Cart` / `SM Cart Item` | `api/cart.py` |
| Order | `Saathi Order` / `Saathi Order Item` | `api/order.py` |
| Coupon | `Saathi Coupon` | `saathi_middleware/doctype/saathi_coupon/saathi_coupon.py` |
| Loyalty | `SM Loyalty Program` / `SM Loyalty Point Entry` | `api/loyalty.py` |
| Payment | `Payment Log` | `api/payments.py` |
| In-app notification | `SM Notification` | `api/notifications.py` |
| Franchise→middleware sync audit trail | `Saathi Sync Log` (on the franchise's own `saathi_ecommerce` site) | `saathi_ecommerce/api/middleware_client.py` |
