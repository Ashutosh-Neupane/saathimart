# Storefront Revalidation & Cache Invalidation (saathimart hub → Next.js)

## What the hub now does automatically

On any change to data the storefront displays, `saathimart.api.storefront_cache`
(doc_events in hooks.py):

1. **Busts the hub's own Redis** (`sm_product:*`, `sm_best_listing:*`,
   `sm_stock:*`, …) so downstream reads never see stale hub data.
2. **POSTs the affected Next.js cache tags** to the storefront's
   `/api/revalidate` route (queued on `short`, never blocks the save,
   failures only logged).

Doctypes wired: Product, Vendor Listing, Vendor Stock, Review, Order
(after_insert/on_update), Site Config, Navigation Item, Banner, Site Page,
Blog Post, FAQ Category/Item, Offer, Popular Location, Hero Slide,
Seasonal Banner, Trust Badge, Product Rail Heading, Homepage Settings,
Website Content, and the static pages (About/Terms/Privacy/Cookies/
Careers/Partner/Rider).

## Settings (SaathiMart Settings → Sync & Webhooks tab)

| Field | Meaning |
|---|---|
| `enable_nextjs_revalidation` | master switch (default off) |
| `nextjs_base_url` | storefront origin, e.g. `http://localhost:3000` |
| `revalidation_secret` | Password field — must equal the storefront's `REVALIDATION_SECRET` env var |

## Webhook contract (matches the FE reference repo's app/api/revalidate/route.ts)

```
POST {nextjs_base_url}/api/revalidate
Headers: x-revalidate-secret: <revalidation_secret>
Body:    {"tags": ["catalog-list", "catalog-product-tea-500g", ...]}
200 → {"revalidated": N, "tags": [...]}
400 → disallowed/unknown tag (allowlist: content-, catalog-, orders-, cart-, location-)
```

### Delivery & retry semantics

Deliveries run on the `short` queue with **bounded exponential backoff**
(default 4 attempts, 1s→2s→4s; worst case ~40s of a 90s job budget):

| Response | Classified | Behaviour |
|---|---|---|
| 2xx | `ok` | done |
| 429 / 5xx / timeout / transport error | `retry` | retried until attempts exhausted, then logged |
| other 4xx (401 bad secret, 400 disallowed tag, 404 wrong route) | `fail` | **no retry** — a contract mismatch can't be fixed by retrying; logged once |

Failures never raise into the caller (a dead storefront can't break a
Frappe save). Smoke tests: `saathimart/tests/test_storefront_revalidation.py`
(mock HTTP receiver proves the wire bytes, the retry classes, and that
permanent rejections are attempted exactly once).

Tag universe emitted by the hub:
- `catalog-list` — any product/listing/filter-level change
- `catalog-product-{slug}` — that product's detail data
- `content-site` — any CMS content change (coarse, safe for all content reads)
- `content-page:{slug}` — specific Site Page
- `cms-faq`, `cms-offers`, `cms-offer-{slug}` — FAQ / offers
- `orders-detail-{id}`, `orders-list-{user}` — order reads

## What the Next.js app must do when cloned for real (reference: saathimart-fe)

1. Reads already tag correctly via `cachedPublicGet(url, [tags], ttl)` —
   `lib/data/catalog.ts` uses `cacheTag.catalog.list()` /
   `catalog-product-{slug}`, `lib/data/offers.ts` uses `cms-offers` /
   `cms-offer-{slug}`, `lib/data/faq.ts` uses `cms-faq`.
2. **One gap to add there:** the generic CMS layer (`lib/content/cms.ts`)
   should also tag reads with `cacheTag.content.site()` so the hub's coarse
   `content-site` bust covers site config / banners / navigation — the hub
   already emits that tag on every CMS change.
3. Set `REVALIDATION_SECRET=<same secret>` in the FE env.

## Manual revalidation (admin)

```
POST /api/method/saathimart.api.webhook.revalidate_nextjs
  {"paths": ["/products/tea-500g"], "tag": "product"}   # roles: SM Admin
POST /api/method/saathimart.api.webhook.revalidate_all_products
```

`webhook.py` now delegates delivery to `storefront_cache.notify_nextjs`
(correct header + tags contract).
