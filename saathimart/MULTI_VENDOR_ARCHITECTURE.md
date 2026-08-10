# SaathiMart Multi-Vendor Architecture — Dependency Map & Refactor Plan

## Current State Analysis

### DocType Dependency Map

```
Product (single-vendor, single-price, single-stock)
├── vendor ────────────────┐
├── price                  │
├── compare_price          │
├── stock_qty              │
├── sku                    │
├── delivery_zone          │
├── thumbnail              │
├── images (JSON)          │
├── short_description      │
├── description            │
├── meta_title             │
├── meta_description       │
├── tags                   │
├── vendor_product_id      │
├── avg_rating             │
├── review_count           │
├── track_inventory        │
├── allow_backorder        │
├── low_stock_threshold    │
├── status                 │
├── slug                   │
├── category               │
└── prices (child table: Product Price)
    └── vendor (optional)

Cart Item
├── product ───────────────┘
├── product_name
├── vendor ────────────────┐
├── qty                    │
├── rate                    │
└── amount                  │
                            │
Order                       │
├── customer_name           │
├── customer_email          │
├── customer_phone          │
├── status                  │
├── payment_status          │
├── payment_method          │
├── items (child: Order Item)
│   ├── product ───────────┤
│   ├── product_name       │
│   ├── sku                │
│   ├── qty                │
│   ├── rate                │
│   ├── amount              │
│   └── vendor ────────────┘
├── subtotal
├── net_total
├── taxes (child: Order Tax)
├── total_taxes
├── delivery_charge
├── free_delivery
├── discount_amount
├── total_discount
├── grand_total
├── delivery_zone
├── delivery_address
├── delivery_lat
├── delivery_lng
├── vendor ─────────────────┘
├── vendor_order_id
├── source_site
├── cart_id
├── notes
├── payment_reference
├── esewa_transaction_uid
├── loyalty_points_redeemed
├── loyalty_discount
├── loyalty_points_earned
├── coupon_code
└── coupon_discount

Vendor
├── vendor_name
├── slug
├── status
├── frappe_site_url
├── api_key
├── api_secret
├── contact_email
├── contact_phone
├── address
├── delivery_zone
├── commission_pct
├── lat
├── lng
├── service_radius_km
├── default_warehouse
├── total_available_qty (read-only)
├── total_physical_qty (read-only)
├── last_sync_at
├── hub_status
└── notes

Vendor Stock (already multi-vendor per product)
├── vendor
├── product
├── available_qty
├── reserved_qty
├── physical_qty
├── last_updated
└── last_sync_at

Product Price (child table on Product)
├── price_type
├── vendor (optional)
├── price
├── min_qty
├── delivery_zone
├── valid_from
├── valid_to
└── is_active
```

### API Dependency Map

```
Products API (products.py)
├── list_products() → returns Product with single vendor/price/stock
├── get_product() → returns Product with single vendor/price/stock
├── lookup_by_barcode() → returns Product
└── get_effective_price() → resolves price from Product.prices child table

Cart API (cart.py)
├── add_to_cart(session_id, product, qty, vendor=None)
│   └── Resolves price via get_effective_price(product_doc, vendor=vendor)
│   └── Stores vendor on Cart Item
├── update_cart_item(session_id, product, qty, vendor=None)
├── get_cart_summary(session_id) → returns items with vendor
├── get_cart_count(session_id)
└── merge_guest_cart(user, guest_session_id)

Orders API (orders.py)
├── checkout(session_id, ..., delivery_zone, coupon_code, loyalty_points, notes, customer_email)
│   ├── Reads cart items (each has vendor)
│   ├── Validates single vendor per cart (MIXED_VENDOR_ERROR)
│   ├── Creates Order with single vendor
│   ├── Creates Order Items
│   ├── Runs totals
│   ├── Reserves stock via stock.atomic_reserve(vendor, product, qty)
│   └── Clears cart
├── list_orders() → filters by SM Customer role email
├── get_order() → returns Order + items
└── track_order() → public tracking

Stock API (stock.py)
├── get_vendor_stock(vendor, product)
├── apply_vendor_stock_event(event, payload)
├── atomic_reserve(vendor, product, qty)
├── release_reservation(vendor, product, qty)
└── confirm_deduction(vendor, product, qty, order_id)

Events API (events.py)
├── poll() → vendor sites poll for events
├── receive() → inbound webhooks from vendors
│   ├── order.new → create Vendor Order
│   ├── order.confirmed → update Order
│   ├── order.dispatched → update Order
│   ├── order.delivered → confirm stock deduction, loyalty points
│   ├── order.cancelled → release reservation
│   ├── stock.receipt → create Stock Ledger Entry
│   └── price.update → upsert Product Price
└── health()

Filters API (filters.py)
├── get_filters() → returns brands/vendors with product counts
└── _count_by_field() → groups products by vendor

Home API (home.py)
├── get_home_content() → assembles home content
└── _get_product_rail_headings() → returns rail titles
```

### Frontend Dependency Map

```
Product Type (components/product-card.tsx)
├── id: string (slug)
├── name: string
├── description: string
├── image: string | StaticImageData
├── price: number
├── originalPrice?: number
├── discountPercent?: number
├── rating: number
├── reviewCount?: number
├── deliveryTime?: string
├── inStock?: boolean
└── href?: string

CartItem Type (lib/types/cart.ts)
├── id: string (slug)
├── name: string
├── packSize: string
├── unitPrice: number
├── originalUnitPrice?: number
├── quantity: number
├── image: StaticImageData | string
├── imageFit: "cover" | "contain"
└── vendor?: string (NEW - added)

Order Type (lib/data/orders.ts)
├── id: string
├── orderNumber: string
├── placedAt: string
├── status: OrderStatus
├── items: OrderLineItem[]
├── deliveryAddress: string
├── grandTotal: number
└── paymentMethod: string

BackendProduct Type (lib/api/index.ts)
├── name: string (internal Product name)
├── product_name: string
├── slug: string
├── category: string
├── vendor?: string (single vendor)
├── status: string
├── price: number (single price)
├── compare_price?: number
├── stock_qty: number (single stock)
├── track_inventory: number
├── allow_backorder: number
├── thumbnail: string
├── images?: string
├── short_description: string
├── description: string
├── meta_title?: string
├── meta_description?: string
├── tags?: string
├── vendor_product_id?: string
├── delivery_zone?: string
├── avg_rating: number
├── review_count: number
└── sku?: string
```

## Proposed Architecture

### New DocTypes

**1. Product (refactored — shared catalog only)**
```
Product
├── product_name (Data, required)
├── slug (Data, unique)
├── category (Link → Category)
├── brand (Data) — NEW
├── status (Select: Draft/Active/Inactive/Out of Stock)
├── short_description (Small Text)
├── description (Text Editor)
├── specifications (Table → Product Specification) — NEW
│   ├── label (Data)
│   └── value (Data)
├── tags (Data)
├── meta_title (Data)
├── meta_description (Small Text)
├── avg_rating (Float, read-only)
├── review_count (Int, read-only)
└── media (Table → Product Media) — NEW
    ├── file (Attach)
    ├── file_type (Select: image/video)
    ├── alt_text (Data)
    ├── is_primary (Check)
    ├── sort_order (Int)
    ├── width (Int)
    └── height (Int)
```

**2. Vendor Listing (NEW — replaces Product.vendor/price/stock)**
```
Vendor Listing
├── vendor (Link → Vendor, required)
├── product (Link → Product, required)
├── status (Select: Active/Inactive/Out of Stock, default: Active)
├── price (Currency, required)
├── compare_price (Currency)
├── sku (Data)
├── vendor_product_id (Data)
├── warehouse (Link → Warehouse)
├── delivery_zone (Link → Delivery Zone)
├── track_inventory (Check, default: 1)
├── allow_backorder (Check)
├── estimated_delivery_minutes (Int)
├── priority (Int, default: 0) — for fallback sorting
├── last_updated (Datetime, read-only)
└── last_sync_at (Datetime, read-only)

Unique constraint: (vendor, product)
```

**3. Product Media (NEW — replaces Product.thumbnail + Product.images JSON)**
```
Product Media
├── product (Link → Product, required)
├── file (Attach, required)
├── file_type (Select: image/video, default: image)
├── alt_text (Data)
├── is_primary (Check, default: 0)
├── sort_order (Int, default: 0)
├── width (Int)
└── height (Int)
```

**4. Product Specification (NEW — structured specs)**
```
Product Specification
├── product (Link → Product, required)
├── label (Data, required)
└── value (Data, required)
```

### What Gets Removed from Product

| Field | Reason | Migration |
|-------|--------|-----------|
| `vendor` | Single-vendor assumption | Move to Vendor Listing |
| `price` | Single-price assumption | Move to Vendor Listing |
| `compare_price` | Single-price assumption | Move to Vendor Listing |
| `stock_qty` | Single-stock assumption | Move to Vendor Listing (via Vendor Stock) |
| `low_stock_threshold` | Single-stock assumption | Move to Vendor Listing |
| `track_inventory` | Single-stock assumption | Move to Vendor Listing |
| `allow_backorder` | Single-stock assumption | Move to Vendor Listing |
| `sku` | Vendor-specific | Move to Vendor Listing |
| `vendor_product_id` | Vendor-specific | Move to Vendor Listing |
| `delivery_zone` | Vendor-specific | Move to Vendor Listing |
| `thumbnail` | Replaced by Product Media | Migrate to Product Media |
| `images` (JSON) | Replaced by Product Media | Migrate to Product Media |
| `prices` (child table) | Replaced by Vendor Listing | Migrate to Vendor Listing |

### What Stays in Product

| Field | Reason |
|-------|--------|
| `product_name` | Shared catalog identity |
| `slug` | URL routing |
| `category` | Shared taxonomy |
| `brand` | Shared catalog |
| `status` | Shared catalog status |
| `short_description` | Shared catalog |
| `description` | Shared catalog |
| `meta_title` | Shared SEO |
| `meta_description` | Shared SEO |
| `tags` | Shared catalog |
| `avg_rating` | Computed across all vendors |
| `review_count` | Computed across all vendors |

### Vendor Listing as Source of Truth

Vendor Listing becomes the single source of truth for:
- Which vendors sell which products
- Each vendor's price, MRP, stock
- Each vendor's warehouse, zone, delivery info
- Each vendor's SKU/vendor product code

Vendor Stock becomes a read-only ledger that is auto-maintained from Vendor Listing stock changes (or we merge Vendor Stock into Vendor Listing).

### Vendor Selection Logic

```
select_best_vendor(product, customer_location, cart_items?)
    candidates = Vendor Listing.find({
        product: product,
        status: "Active"
    })
    
    if candidates.length == 0:
        return null
    
    if candidates.length == 1:
        return candidates[0]
    
    # Sort by configurable priority
    # 1. Nearest by distance (if customer_location provided)
    # 2. Has stock (available_qty > 0)
    # 3. Lowest delivery cost
    # 4. Lowest delivery time
    # 5. Lowest price
    # 6. Vendor priority (fallback)
    
    scored = candidates.map(v => ({
        vendor: v,
        score: compute_score(v, customer_location)
    }))
    
    return scored.sort(score asc)[0].vendor
```

### Cart Logic Changes

Current: Cart Item has vendor set at add-to-cart time
New: Cart Item vendor is auto-assigned if not provided, or validated on checkout

```
add_to_cart(session_id, product_slug, qty, vendor=None)
    if vendor is None:
        vendor = select_best_vendor(product, customer_location)
    
    # Check stock for selected vendor
    vendor_listing = Vendor Listing.get(vendor, product)
    if vendor_listing.available_qty < qty:
        throw "Insufficient stock"
    
    # Add to cart with resolved vendor
```

### Order Splitting Architecture

```
Order (customer-facing)
├── order_number
├── customer_name
├── customer_email
├── customer_phone
├── delivery_address
├── delivery_lat
├── delivery_lng
├── status
├── payment_status
├── payment_method
├── grand_total
├── items (Order Item)
│   ├── product
│   ├── product_name
│   ├── qty
│   ├── rate
│   ├── amount
│   ├── vendor
│   └── vendor_listing (Link → Vendor Listing) — NEW
├── vendor_fulfillments (child table) — NEW
│   ├── vendor
│   ├── vendor_order_id
│   ├── status
│   ├── subtotal
│   └── items_count
└── ...
```

Each vendor fulfillment can have its own status, tracking, and vendor order ID.

### Migration Strategy

1. Create new doctypes (Vendor Listing, Product Media, Product Specification)
2. Create migration script:
   - For each Product with vendor: create Vendor Listing row
   - Copy price, compare_price, stock_qty, sku, delivery_zone to Vendor Listing
   - Copy thumbnail/images to Product Media
   - Remove old fields from Product
3. Update APIs to use Vendor Listing
4. Update frontend types
5. Run migration on existing data
6. Deploy

### Backward Compatibility

- Keep `Product.vendor` as a deprecated computed field (read-only, from first Vendor Listing)
- Keep `Product.price` as a deprecated computed field (read-only, from best Vendor Listing)
- Keep `Product.stock_qty` as a deprecated computed field (read-only, sum of all Vendor Listings)
- Old APIs continue to work but return computed values
- Frontend gradually migrates to new types