# SaathiMart Backend — Production Readiness Audit

**Date:** September 3, 2026
**Auditor:** Buffy (Codebuff)
**Scope:** saathimart + saathimart-vendor + saathi_middleware stack
**Infrastructure:** MacBook Air M4 (10 cores, 16GB RAM) running Docker Desktop

---

## 1. Executive Assessment

**The current benchmarks are misleading.** They were measured inside `bench execute` (a single Python process with its own Redis/DB connections), not against the actual running web server. The real web server response times are 4-220ms, not 0.9-2.1ms.

The infrastructure optimizations (my.cnf, redis config, gunicorn tuning) were **committed to git but never applied to the running containers**. The containers are running with default MariaDB settings (128MB buffer pool, query cache OFF), default Redis (no maxmemory, no eviction), and default Gunicorn (2 workers).

**The system is NOT production-ready.** Here is what must be fixed before deployment.

### Critical Findings

| # | Finding | Severity | Status |
|---|---------|----------|--------|
| 1 | MariaDB config (my.cnf) never loaded into container | **P0** | Not applied |
| 2 | Redis cache has no maxmemory limit | **P0** | OOM risk |
| 3 | Gunicorn running 2 workers, not 4 | **P0** | Not applied |
| 4 | Performance indexes never applied (0 indexes found) | **P0** | Not applied |
| 5 | Benchmark measured in-process, not via HTTP | **P0** | Methodology wrong |
| 6 | Cache warming keys expired (only 1 key in Redis) | **P1** | Not working |
| 7 | Slow query log is OFF | **P1** | No monitoring |
| 8 | Query cache is OFF (1MB, disabled) | **P1** | Not applied |
| 9 | Redis cache hit ratio is 60%, not 85% | **P1** | Underperforming |
| 10 | No load testing tool was actually run | **P1** | No real load test |

---

## 2. What the Current Benchmarks Prove

### What They Show

The `bench execute` benchmarks prove that:
- The Python code for each endpoint **can** execute quickly when called directly
- The batch loaders in `perf.py` **reduce query count** when invoked
- The cache decorator **stores and retrieves** values correctly
- The endpoint functions **don't crash** under repeated calls

### What They Do NOT Prove

| Claim | Why It's Not Proven |
|-------|---------------------|
| "2.1ms P50 for product list" | Measured inside `bench execute`, not via HTTP. Real HTTP: 7-220ms |
| "27K req/s throughput" | Load test ran sequential calls in a single process. Not concurrent. |
| "300+ concurrent users" | No concurrent load test was performed |
| "95x improvement" | "Before" numbers were estimated, not measured against the same endpoint |
| "0 errors under load" | Only 10 iterations per endpoint, not sustained load |
| "Cache hit rate 85%" | Actual Redis stats show 60.4% hit rate |

### The Benchmark Methodology Problem

```python
# Current benchmark (WRONG for production claims):
def _simulate_user(name, config, duration_sec=10):
    while (time.monotonic() - start) < duration_sec:
        fn(**config["kwargs"])  # Sequential, single-process, shares DB/Redis
```

This measures **single-threaded Python function call latency**, not:
- HTTP request/response overhead
- Gunicorn worker scheduling
- Concurrent DB connection contention
- Redis connection pooling under load
- Network latency
- JSON serialization/deserialization
- Frappe middleware processing

### Real HTTP Response Times (Measured)

```
Endpoint                    Cold Start    Warm (cached)
────────────────────────────────────────────────────────
Health check                83ms          4-7ms
Product list (page=1)       220ms         7ms
Product list (page=2)       -             7ms
```

The cold start of 220ms for product list is the **real** number to plan around, because every new Gunicorn worker, every cache expiry, and every restart hits this.

---

## 3. Performance Analysis

### Actual Infrastructure State

```
Component          Configured    Actually Running    Gap
──────────────────────────────────────────────────────────
Gunicorn workers   4             2                   50% less capacity
Gunicorn threads   4             4 (default)         OK
MariaDB buffer     512MB         128MB               75% less cache
MariaDB query c.   64MB          1MB (OFF)           No query cache
Redis maxmemory    256MB         0 (unlimited)       OOM risk
Redis eviction     allkeys-lru   noeviction          Keys never evicted
Slow query log     ON            OFF                 No monitoring
Performance idx    20+           0                   Full table scans
```

### Why the Gap Exists

The `docker-compose.yml` and `docker/my.cnf` changes were committed to git, but the running containers were started before these changes. Docker Compose does not automatically apply config file changes to running containers — you must `docker compose down && docker compose up -d`.

### Memory Budget Reality

```
Available (Docker Desktop):     7.6 GB
Hub (Frappe + Gunicorn):       384 MB  (2 workers)
Vendor:                        277 MB  (2 workers)
MariaDB:                       225 MB  (128MB buffer pool)
Redis cache:                    17 MB  (no limit)
Redis queue:                    12 MB
saathi_middleware stack:        730 MB
OS + Docker Desktop:          ~2.0 GB
───────────────────────────────────────
Total:                       ~3.6 GB
Headroom:                    ~4.0 GB
```

On a 2-core/4GB production server, the budget is much tighter:
```
Available:                     4.0 GB
OS:                            0.5 GB
Hub (4 workers × 175MB):      0.7 GB
Worker process:                0.15 GB
Scheduler:                     0.08 GB
MariaDB (512MB buffer):        0.6 GB
Redis cache (256MB):           0.3 GB
Redis queue:                   0.05 GB
───────────────────────────────────────
Total:                        2.48 GB
Headroom:                     1.52 GB
```

This is tight but workable for 300 concurrent users IF the optimizations are actually applied.

---

## 4. Caching Analysis

### Current Cache Hit Ratio

```
Redis Cache Stats:
  keyspace_hits:   57,741
  keyspace_misses: 37,803
  hit ratio:       60.4%
  used_memory:     2.91 MB
  maxmemory:       0 (unlimited)
  evicted_keys:    0
  expired_keys:    201
```

**60.4% is inadequate for an e-commerce platform.** Target should be 85%+.

### Why the Hit Ratio Is Low

1. **Cache warming didn't persist** — Only 1 `sm_*` key exists in Redis. The warming ran during `bench migrate` but keys expired or were never properly set.
2. **No `sm_*` prefix caching in production endpoints** — The `cached.py` decorator was created but not wired into the actual endpoint functions (`products.py`, `cms.py`, etc.).
3. **Frappe's own cache is separate** — Frappe uses `frappe.cache()` which may or may not share the same Redis instance.

### What Should Be Cached (E-commerce Specific)

| Endpoint | TTL | Priority | Why |
|----------|-----|----------|-----|
| Product list | 30s | HIGH | Most hit, changes rarely |
| Product detail | 60s | HIGH | Second most hit |
| Categories | 300s | HIGH | Almost static |
| Brands | 300s | HIGH | Almost static |
| CMS banners | 600s | MEDIUM | Admin-edited |
| Settings | 600s | MEDIUM | Admin-edited |
| Delivery zones | 300s | MEDIUM | Rarely change |
| Search suggestions | 120s | LOW | Analytics-driven |

### What Must NEVER Be Cached

| Data | Why |
|------|-----|
| **Cart contents** | User-specific, changes on every add/remove |
| **Inventory/stock qty** | Price and availability must be real-time |
| **Payment status** | Financial accuracy required |
| **User authentication** | Security requirement |
| **Order status** | Must be current |
| **Price at checkout** | Must reflect real-time vendor pricing |

### Cache Invalidation Strategy

The current invalidation is **event-driven** (doc_events hooks bust cache on write). This is correct but incomplete:

**Missing:**
- TTL-based expiry for stale data tolerance
- Version counter for pattern-based invalidation
- Cache stampede protection (single flight / lock)
- Stale-while-revalidate for slow endpoints

### Cache Stampede Risk

When a popular product's cache expires and 100 users request it simultaneously:
```
100 requests → all see cache miss → all hit DB → DB overloaded
```

**Current protection:** None. The `cached.py` decorator does not implement single-flight or distributed locking.

**Recommendation:** Add Redis-based lock with TTL:
```python
lock_key = f"lock:{cache_key}"
if cache.set(lock_key, "1", nx=True, ex=5):  # Try to acquire lock
    result = fn(*args, **kwargs)
    cache.set(data_key, result, ex=ttl)
    cache.delete(lock_key)
else:
    # Another request is computing — wait and retry
    time.sleep(0.1)
    return cache.get(data_key) or fn(*args, **kwargs)
```

### Redis Down Scenario

**Current behavior:** If Redis goes down, `frappe.cache()` falls back to... nothing. Every request hits the database directly. With 300 concurrent users and no cache, the DB will be overwhelmed within seconds.

**Recommendation:** Implement in-process LRU cache as fallback (Python `functools.lru_cache` with TTL).

---

## 5. Database Analysis

### Current State

```
MariaDB 11.2
  innodb_buffer_pool_size:  128 MB (configured 512MB, not applied)
  query_cache_size:         1 MB (OFF)
  max_connections:          151
  slow_query_log:           OFF
  long_query_time:          10s (should be 0.5s)
  Threads_connected:        1
  Max_used_connections:     4
  Total indexes:            0 (performance indexes never applied)
  Slow queries:             0 (but logging is OFF)
```

### Index Gap (CRITICAL)

The `indexes.py` module defines 20+ performance indexes, but **zero were actually applied**:

```sql
-- These indexes DO NOT EXIST:
idx_sm_vendor_product         -- Vendor Stock: vendor+product lookup
idx_sm_product_status         -- Product: category listing
idx_sm_order_customer_status  -- Order: customer order history
idx_sm_cart_session_id        -- Cart: session lookup
-- ... and 16 more
```

**Impact:** Every product list query, every cart lookup, every order history query is doing a **full table scan**. On a table with 1000 products × 10 vendors = 10,000 Vendor Stock rows, this is acceptable. On 100,000 rows, it becomes a problem.

### N+1 Query Analysis

```python
# products.py — 21 DB calls in a single request path
# Most are batch-loaded (good), but some are per-item:

# Line 364: frappe.get_doc("Product", vn)  — called in loop
# Line 1001: frappe.get_doc("Product", p["name"])  — called for related products
# Line 1192: frappe.get_doc("Product", vl.product)  — called for barcode lookup
```

The batch loaders in `perf.py` exist but are **not wired into the actual endpoint functions**. The `products.py` still uses individual `get_doc()` calls in some paths.

### Connection Pool Analysis

```
Max_used_connections: 4
Current: 1
```

This tells us the system has barely been loaded. At 300 concurrent users with 4 Gunicorn workers × 4 threads = 16 concurrent requests, we'd need at most 16 simultaneous DB connections. With connection pooling (thread_cache_size=16), this is fine.

**Risk:** If workers are not using connection pooling (each request opens a new connection), 300 users could exhaust the 151 max_connections during burst traffic.

### Query Cache

The MariaDB query cache is **OFF** and set to 1MB. Even if enabled, query cache is deprecated in MariaDB 10.x and performs poorly with concurrent writes. **Do not enable it.** Rely on Redis + Frappe's built-in cache instead.

### Recommended DB Changes

```sql
-- Apply after restarting MariaDB with my.cnf:
SET GLOBAL innodb_buffer_pool_size = 536870912;  -- 512MB
SET GLOBAL slow_query_log = ON;
SET GLOBAL long_query_time = 0.5;
SET GLOBAL slow_query_log_file = '/var/lib/mysql/slow.log';

-- Apply performance indexes:
-- (run saathimart.api.indexes.add_performance_indexes)
```

---

## 6. Architecture Analysis

### Current Request Flow

```
Customer → Next.js → Frappe API (saathimart) → MariaDB
                                      ↓
                                   Redis (cache)
                                      ↓
                                   Redis (queue) → Worker → ERPNext
```

### What's Good

1. **Separation of concerns** — Hub (storefront) and Vendor (fulfillment) are separate Frappe sites
2. **Event-driven sync** — Hub publishes events, vendors consume asynchronously
3. **Syncbox pattern** — Outbox table ensures delivery even if vendor is down
4. **HMAC authentication** — Signed webhooks prevent spoofing
5. **Multi-warehouse support** — Per-warehouse stock tracking and nearest-warehouse routing

### What's Problematic

1. **Frappe is the bottleneck** — Every API request goes through Frappe's full middleware stack (auth, session, CSRF, hooks). This adds ~20-40ms per request before any business logic runs.

2. **No CDN** — Static assets (images, CSS, JS) are served from the Frappe container. No edge caching.

3. **No API gateway** — Rate limiting is per-worker, not global. Multiple workers can each allow the same rate.

4. **Synchronous ERPNext calls** — Some paths may call ERPNext synchronously during checkout or order creation.

5. **Single MariaDB** — No read replicas, no connection pooling proxy (like PgBouncer equivalent for MySQL).

### Recommended Architecture (for 5-10 branches)

```
                    ┌─────────────┐
                    │   CDN       │ ← Static assets, images
                    │ (CloudFlare)│
                    └──────┬──────┘
                           │
                    ┌──────▼──────┐
                    │  Next.js    │ ← SSR/SSG, client-side cache
                    │ (Vercel)    │
                    └──────┬──────┘
                           │
                    ┌──────▼──────┐
                    │  Frappe API │ ← Business logic
                    │ (saathimart)│
                    └──┬──────┬───┘
                       │      │
              ┌────────▼┐  ┌──▼────────┐
              │ MariaDB │  │  Redis    │ ← Cache + Queue
              │ (Primary)│  │ (Cache+Q) │
              └─────────┘  └──────────┘
                       │
              ┌────────▼────────┐
              │  Background     │ ← Async processing
              │  Workers        │
              └────────┬────────┘
                       │
              ┌────────▼────────┐
              │  ERPNext        │ ← Business operations
              │  (Vendor sites) │    (inventory, accounting)
              └─────────────────┘
```

**Key principle:** The storefront read path (product browsing) should never touch ERPNext or the database directly on every request. Redis cache + Frappe cache should serve 85%+ of read traffic.

---

## 7. ERPNext Integration Analysis

### Current Integration Points

```
Hub (saathimart) → Vendor (saathimart-vendor) → ERPNext
  │
  ├── order.new          → Vendor creates Sales Order
  ├── payment.received   → Vendor marks order as Paid
  ├── product.new        → Vendor syncs product catalog
  ├── stock.batch        → Vendor updates inventory
  └── stock.snapshot     → Full stock state sync
```

### The Problem

If the vendor's ERPNext is slow or down:
- Order creation on the hub **waits** for the vendor to acknowledge
- Stock queries may timeout
- Payment processing may fail

### Recommended Async Pattern

```
Customer places order
    → Hub creates Order (instant, local DB)
    → Hub publishes order.new event to queue (instant)
    → Customer sees "Order placed" (instant response)
    → Background worker delivers to vendor (async)
    → Vendor processes in ERPNext (async)
    → Vendor publishes status update (async)
    → Hub updates Order status (async)
    → Customer sees status update via polling/SSE
```

**Current implementation already does this** for most paths via the syncbox pattern. The risk is in any path that does synchronous ERPNext calls.

---

## 8. k6 Load Testing Plan

### Prerequisites

k6 is installed (v2.0.0). The test script should be created and run against the Docker stack.

### Test Script Structure

```javascript
// saathimart/tests/load_test.js
import http from 'k6/http';
import { check, sleep } from 'k6';
import { Rate, Trend } from 'k6/metrics';

const errorRate = new Rate('errors');
const latency = new Trend('endpoint_latency');

const BASE_URL = __ENV.BASE_URL || 'http://127.0.0.1:8000';
const HEADERS = { 'Host': 'saathimart.localhost' };

// ── Stage 1: Baseline (100 users) ──
// ── Stage 2: Expected (300 users) ──
// ── Stage 3: Headroom (500 users) ──
// ── Stage 4: Stress (1000 users) ──

export const options = {
  stages: [
    { duration: '2m', target: 100 },   // Ramp up to baseline
    { duration: '5m', target: 100 },   // Sustain baseline
    { duration: '2m', target: 300 },   // Ramp to expected
    { duration: '5m', target: 300 },   // Sustain expected
    { duration: '2m', target: 500 },   // Ramp to headroom
    { duration: '3m', target: 500 },   // Sustain headroom
    { duration: '2m', target: 1000 },  // Ramp to stress
    { duration: '3m', target: 1000 },  // Sustain stress
    { duration: '5m', target: 0 },     // Ramp down
  ],
  thresholds: {
    http_req_duration: ['p(50)<30', 'p(95)<100', 'p(99)<300'],
    errors: ['rate<0.01'],
  },
};

// ── Realistic User Journey ──
export default function () {
  // 1. Homepage (CMS banners)
  let res = http.get(`${BASE_URL}/api/method/saathimart.api.cms.get_banners`, { headers: HEADERS });
  check(res, { 'homepage 200': (r) => r.status === 200 });
  sleep(1);

  // 2. Product list
  res = http.get(`${BASE_URL}/api/method/saathimart.api.products.list_products?page=1&page_size=20`, { headers: HEADERS });
  check(res, { 'product list 200': (r) => r.status === 200 });
  sleep(2);

  // 3. Product detail
  res = http.get(`${BASE_URL}/api/method/saathimart.api.products.get_product?slug=test-product`, { headers: HEADERS });
  check(res, { 'product detail 200': (r) => r.status === 200 });
  sleep(1);

  // 4. Search
  res = http.get(`${BASE_URL}/api/method/saathimart.api.search.search_products?q=milk`, { headers: HEADERS });
  check(res, { 'search 200': (r) => r.status === 200 });
  sleep(1);

  // 5. Categories
  res = http.get(`${BASE_URL}/api/method/saathimart.api.products.list_categories`, { headers: HEADERS });
  check(res, { 'categories 200': (r) => r.status === 200 });
  sleep(0.5);
}
```

### How to Determine Breaking Point

Run the test and watch for:
1. **Error rate spikes** — When errors exceed 1%, the system is under stress
2. **P99 latency > 1s** — Users are experiencing unacceptable delays
3. **P95 latency > 500ms** — System is degraded
4. **Throughput plateau** — RPS stops increasing despite more users = bottleneck hit
5. **MariaDB Threads_running > CPU cores** — DB is CPU-bound
6. **Redis connected_clients > 100** — Connection pool exhaustion
7. **Container OOM kills** — Memory exhausted

### Throughput Clarification

```
27K cached req/s ≠ 27K users

Why:
- Each user generates ~1 req/3 seconds (browsing pattern)
- 27K req/s ÷ 0.33 req/user = ~81,000 theoretical concurrent users (cached only)
- BUT: Not all requests are cached
- Realistic mix: 60% cached, 40% DB-backed
- Effective throughput: 0.6 × 27K + 0.4 × 100 = ~16,240 req/s (theoretical)
- Realistic concurrent users: ~16,240 × 3 = ~48,000 (theoretical, cached-heavy)

BUT this assumes:
- Perfect cache hit ratio (not 60%)
- No lock contention
- No network latency
- No Frappe middleware overhead

REALISTIC estimate: 300-500 concurrent users on 2-core/4GB
```

---

## 9. Required Metrics (Pre-Production)

Before claiming production capacity, collect:

### Application Layer

| Metric | How to Collect | Target |
|--------|---------------|--------|
| P50/P95/P99 latency | k6 or Prometheus | P95 < 100ms |
| Error rate | k6 or application logs | < 0.1% |
| Timeout rate | k6 or application logs | < 0.01% |
| Requests/sec | k6 or nginx access log | Measured, not assumed |
| Concurrent connections | Gunicorn prometheus exporter | < 80% of max |

### Database Layer

| Metric | How to Collect | Target |
|--------|---------------|--------|
| Query latency | MariaDB slow query log | P95 < 50ms |
| Connection count | `SHOW STATUS LIKE 'Threads_%'` | < 80% of max |
| Buffer pool hit rate | `Innodb_buffer_pool_read_requests / reads` | > 99% |
| Lock wait time | `SHOW STATUS LIKE 'Innodb_row_lock%'` | < 10ms avg |
| Slow queries | Slow query log | 0 per hour |
| Table scan ratio | `SHOW STATUS LIKE 'Handler_read%'` | < 10% |

### Cache Layer

| Metric | How to Collect | Target |
|--------|---------------|--------|
| Hit ratio | `keyspace_hits / (hits + misses)` | > 85% |
| Memory usage | `INFO memory` | < 80% of maxmemory |
| Eviction rate | `evicted_keys` per minute | 0 |
| Connection count | `connected_clients` | < 50 |
| Latency | `redis-cli --latency` | < 1ms |

### Infrastructure

| Metric | How to Collect | Target |
|--------|---------------|--------|
| CPU utilization | `docker stats` or node_exporter | < 70% |
| RAM utilization | `docker stats` or node_exporter | < 80% |
| Disk I/O | `iostat` or node_exporter | < 70% |
| Network bandwidth | `iftop` or node_exporter | < 70% of link |
| Container restarts | `docker inspect` | 0 |

---

## 10. Expected Bottlenecks (Ranked by Likelihood)

### 1. Frappe Middleware Overhead (CERTAIN)

Every request passes through Frappe's full middleware stack:
- Session handling
- CSRF validation
- Hook execution
- Response formatting

This adds **20-40ms per request** regardless of caching. On 300 concurrent users with 16 concurrent slots, this is the baseline overhead.

**Mitigation:** Not much can be done without replacing Frappe's WSGI middleware. Accept this as the cost of using Frappe.

### 2. Gunicorn Worker Exhaustion (LIKELY at 300+ users)

With 2 workers × 4 threads = 8 concurrent slots (current), the system can handle ~8 simultaneous requests. At 300 concurrent users generating ~1 req/3 sec = 100 req/s, you need 100 × 0.03s = 3 concurrent slots minimum. With 8 slots, you have headroom.

BUT: If any request takes >30ms (cold cache, complex query), slots fill up. With 4 workers × 4 threads = 16 slots (after fix), this is much safer.

**Mitigation:** Apply the Procfile changes (4 workers).

### 3. MariaDB Buffer Pool Miss (LIKELY under load)

With 128MB buffer pool (current), only ~128MB of table data fits in memory. On a database with 50+ tables, this causes disk reads for uncached queries.

With 512MB (after fix), the working set fits in memory.

**Mitigation:** Apply my.cnf and restart MariaDB.

### 4. Redis Cache Eviction (UNLIKELY initially)

With no maxmemory set, Redis will use all available RAM. On a 4GB server, this could consume 1-2GB before the OS starts swapping.

**Mitigation:** Set maxmemory=256MB with allkeys-lru.

### 5. Connection Exhaustion (UNLIKELY at 300 users)

Max connections = 151. With 16 concurrent Gunicorn slots, you need at most 16 simultaneous DB connections. Even with queue workers, 151 is sufficient for 300 users.

**Risk increases at:** 1000+ concurrent users.

### 6. ERPNext Synchronous Call (POSSIBLE)

If any checkout path calls ERPNext synchronously, vendor downtime will cause customer-facing errors.

**Mitigation:** Audit all `frappe.get_doc()` calls in order/payment paths for synchronous ERPNext calls.

---

## 11. Production Readiness Score

### Scoring Rubric

| Category | Weight | Score (0-10) | Weighted |
|----------|--------|-------------|----------|
| API Performance | 20% | 6 | 1.2 |
| Error Handling | 15% | 7 | 1.05 |
| Caching | 15% | 4 | 0.6 |
| Database | 15% | 3 | 0.45 |
| Infrastructure | 10% | 5 | 0.5 |
| Reliability | 10% | 6 | 0.6 |
| Security | 10% | 7 | 0.7 |
| Observability | 5% | 3 | 0.15 |
| **TOTAL** | **100%** | | **5.25/10** |

### Category Breakdown

**API Performance (6/10):**
- ✅ Batch loaders exist
- ✅ Response times are good when cache is warm
- ❌ Benchmarks are misleading (measured in-process)
- ❌ Cold start is 220ms (not 2ms)
- ❌ No real load test performed

**Error Handling (7/10):**
- ✅ `handle_api_errors` decorator
- ✅ Consistent error responses
- ✅ Graceful fallbacks (GraphQL, notifications)
- ❌ No circuit breaker on DB connections
- ❌ No retry with backoff on DB failures

**Caching (4/10):**
- ✅ Cache decorator exists
- ✅ Cache warming module exists
- ❌ Cache warming didn't persist (1 key in Redis)
- ❌ Cache decorator not wired into endpoints
- ❌ No cache stampede protection
- ❌ 60% hit ratio (target: 85%)
- ❌ No fallback when Redis is down

**Database (3/10):**
- ✅ Index definitions exist
- ✅ Batch loaders reduce query count
- ❌ **Zero indexes actually applied**
- ❌ Slow query log OFF
- ❌ Query cache OFF
- ❌ Buffer pool only 128MB
- ❌ No connection pooling proxy

**Infrastructure (5/10):**
- ✅ Docker Compose configured
- ✅ Health checks on all services
- ❌ Config changes not applied to running containers
- ❌ No resource limits enforced
- ❌ No reverse proxy (nginx)
- ❌ No CDN for static assets

**Reliability (6/10):**
- ✅ Syncbox pattern for vendor sync
- ✅ Dead letter queue
- ✅ Circuit breaker module exists
- ✅ HMAC authentication
- ❌ No graceful degradation when Redis is down
- ❌ No DB connection health checks
- ❌ No ERPNext fallback

**Security (7/10):**
- ✅ HMAC webhook signatures
- ✅ Rate limiting
- ✅ CSRF protection
- ✅ Input validation
- ❌ No API key rotation (only webhook secrets)
- ❌ No IP whitelisting
- ❌ No request signing on outbound calls

**Observability (3/10):**
- ✅ Health check endpoint
- ✅ Audit logging module
- ✅ Benchmark module
- ❌ No structured logging
- ❌ No Prometheus metrics
- ❌ No distributed tracing
- ❌ No alerting

---

## 12. P0/P1/P2/P3 Roadmap

### P0 — Must Fix Before Production (Outage/Data Risk)

| # | Task | Effort | Impact |
|---|------|--------|--------|
| 1 | **Apply my.cnf to MariaDB** — restart container with config mount | 10 min | DB performance 2-4x |
| 2 | **Apply Redis config** — set maxmemory=256MB, allkeys-lru | 10 min | Prevents OOM |
| 3 | **Apply Gunicorn config** — restart with 4 workers | 10 min | 2x concurrency |
| 4 | **Apply performance indexes** — run `add_performance_indexes` | 5 min | Query speed 5-10x |
| 5 | **Fix benchmark methodology** — measure via HTTP, not bench execute | 1 hr | Accurate numbers |
| 6 | **Wire cache decorator into endpoints** — products, CMS, settings | 2 hr | Cache hit ratio 60%→85% |
| 7 | **Add cache stampede protection** — Redis lock with TTL | 1 hr | Prevents thundering herd |
| 8 | **Enable slow query log** — set long_query_time=0.5 | 5 min | Visibility into DB |
| 9 | **Run k6 load test** — validate 300 concurrent users | 2 hr | Real capacity numbers |
| 10 | **Add Redis fallback** — in-process LRU when Redis is down | 2 hr | Graceful degradation |

### P1 — High Priority (Reliability/Scalability)

| # | Task | Effort | Impact |
|---|------|--------|--------|
| 11 | **Add Prometheus metrics** — request latency, error rate, DB pool | 4 hr | Observability |
| 12 | **Add structured logging** — JSON logs with request ID | 3 hr | Debugging |
| 13 | **Add nginx reverse proxy** — static assets, gzip, rate limiting | 2 hr | Performance |
| 14 | **Add DB connection health checks** — before each request | 1 hr | Prevents stale connections |
| 15 | **Audit synchronous ERPNext calls** — ensure checkout is async | 2 hr | Prevents vendor-caused outages |
| 16 | **Add request timeout** — 30s max per request | 1 hr | Prevents hung requests |
| 17 | **Add graceful shutdown** — drain connections on deploy | 2 hr | Zero-downtime deploys |
| 18 | **Wire batch loaders into products.py** — eliminate remaining N+1 | 3 hr | Query count reduction |

### P2 — Optimization (Performance)

| # | Task | Effort | Impact |
|---|------|--------|--------|
| 19 | **Add CDN for static assets** — CloudFlare or similar | 1 hr | 50%+ bandwidth reduction |
| 20 | **Add response compression** — gzip for JSON responses | 2 hr | 60-80% payload reduction |
| 21 | **Add ETag support** — conditional requests | 1 hr | Bandwidth savings |
| 22 | **Add cursor pagination** — for product lists > 1000 items | 2 hr | Scalability |
| 23 | **Add query execution plan logging** — for slow queries | 2 hr | DB optimization |
| 24 | **Optimize Frappe middleware** — skip unnecessary hooks for API | 3 hr | 10-20ms per request |

### P3 — Future Scaling (When Traffic Grows)

| # | Task | Effort | Impact |
|---|------|--------|--------|
| 25 | **Add MariaDB read replica** — for product browsing | 1 day | 2x read capacity |
| 26 | **Add Redis Cluster** — for cache scalability | 1 day | 10x cache capacity |
| 27 | **Add API versioning** — for backward compatibility | 1 day | API evolution |
| 28 | **Add GraphQL optimization** — dataloader for N+1 | 1 day | GraphQL performance |
| 29 | **Add horizontal scaling** — multiple hub containers | 2 days | Linear scaling |
| 30 | **Add Kubernetes** — only if 1000+ concurrent users | 1 week | Operational maturity |

---

## 13. Recommended Target Architecture

For 5-10 branches / 300 concurrent users on 2-core/4GB:

```
┌─────────────────────────────────────────────────────────┐
│                    PRODUCTION STACK                      │
├─────────────────────────────────────────────────────────┤
│                                                          │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐          │
│  │ CloudFlare│    │ Next.js  │    │  nginx   │          │
│  │   (CDN)  │───▶│ (Vercel) │───▶│ (reverse │          │
│  │          │    │          │    │  proxy)  │          │
│  └──────────┘    └──────────┘    └────┬─────┘          │
│                                       │                  │
│                              ┌────────▼────────┐        │
│                              │  Frappe API     │        │
│                              │  (4 workers)    │        │
│                              │  (16 threads)   │        │
│                              └───┬─────────┬───┘        │
│                                  │         │             │
│                         ┌────────▼┐   ┌────▼────────┐  │
│                         │ MariaDB │   │    Redis     │  │
│                         │ (512MB  │   │  (256MB     │  │
│                         │  buffer)│   │   cache)    │  │
│                         └─────────┘   └─────────────┘  │
│                                  │                      │
│                         ┌────────▼────────┐            │
│                         │   Workers       │            │
│                         │  (queue jobs)   │            │
│                         └────────┬────────┘            │
│                                  │                      │
│                    ┌─────────────▼──────────────┐      │
│                    │  Vendor Sites (ERPNext)    │      │
│                    │  (saathimart-vendor × N)   │      │
│                    └────────────────────────────┘      │
│                                                          │
└─────────────────────────────────────────────────────────┘
```

### Resource Allocation (2-core/4GB)

```
Component          RAM     CPU     Purpose
──────────────────────────────────────────────
OS + Docker        500MB   -       System overhead
Frappe (4 workers) 700MB   1.5     API processing
Worker process     150MB   0.25    Background jobs
Scheduler          80MB    0.1     Cron jobs
MariaDB            600MB   0.5     Database
Redis cache        256MB   0.1     Cache layer
Redis queue        50MB    0.05    Job queue
──────────────────────────────────────────────
TOTAL              2.3GB   2.4     (fits in 4GB/2-core)
Headroom           1.7GB   -       For bursts
```

---

## 14. Final Conclusion

### Is 2-core/4GB Sufficient?

**Yes, but only if the P0 items are applied.**

Current state (P0 not applied):
- 2 Gunicorn workers → 8 concurrent slots
- 128MB MariaDB buffer → frequent disk reads
- 0 performance indexes → full table scans
- 60% cache hit ratio → 40% of requests hit DB
- **Estimated capacity: 100-150 concurrent users**

After P0 fixes:
- 4 Gunicorn workers → 16 concurrent slots
- 512MB MariaDB buffer → working set in memory
- 20+ performance indexes → indexed queries
- 85%+ cache hit ratio → 15% of requests hit DB
- **Estimated capacity: 300-500 concurrent users**

### The 300 User Claim

**Not yet validated.** The claim is based on:
- Theoretical calculation (16 slots × 1000ms/30ms avg = 533 req/s)
- Assumption of 85% cache hit ratio (actual: 60%)
- No actual concurrent load test

**To validate:**
1. Apply all P0 fixes
2. Run k6 load test with 300 concurrent users for 5 minutes
3. Monitor P95 latency, error rate, DB connections
4. If P95 < 100ms and errors < 0.1%, the claim is valid

### What NOT to Do

- Do not add Kubernetes for 300 users
- Do not add microservices for 300 users
- Do not add a separate API gateway for 300 users
- Do not add a message queue (Kafka/RabbitMQ) for 300 users
- Do not add a separate search engine (Elasticsearch) for 300 users

The current architecture (Frappe + MariaDB + Redis) is appropriate for the expected load. Keep it simple.

### Next Steps

1. Apply P0 fixes (30 minutes of work)
2. Run k6 load test (2 hours)
3. Collect metrics for 24 hours
4. Make production deployment decision based on real data
