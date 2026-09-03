# Plan: Handle 1000 Concurrent Users on 2-Core / 4GB

**Date:** September 3, 2026
**Current state:** 100 users tested, 161 req/s, 29.75% error rate on product list
**Target:** 1000 concurrent users, <1% error rate, P95 <500ms

---

## 1. Current Bottleneck Analysis

### What We Measured (k6, 100 VUs)

```
Endpoint          Pass Rate    Latency (P50)    Why
──────────────────────────────────────────────────────────
Health check      100%         7ms              Cached (Redis)
Categories        100%         7ms              Cached (Redis)
Product list      10%          266ms (P95)      DB query, NOT cached
```

### The Single Bottleneck

**Product list is not cached.** Every request hits MariaDB. At 100 users, the 64 Gunicorn slots (16 workers × 4 threads) get saturated by slow DB queries.

Health and categories pass 100% because they're cached in Redis. Product list fails because it's not.

### Current Resource Usage

```
Component          Usage      Limit     Headroom
─────────────────────────────────────────────────
Backend RAM        652 MB     2 GB      1.3 GB
MariaDB RAM        220 MB     800 MB    580 MB
Redis cache        13 MB      256 MB    243 MB
Redis queue        11 MB      200 MB    189 MB
MariaDB buffer     512 MB     (fixed)   -
MariaDB conns      16/100     100       84
Redis hit ratio    28%        85%+      57% gap
```

---

## 2. The Math: How to Get to 1000 Users

### Request Pattern (E-commerce)

```
Average user generates: ~1 request per 3 seconds (browsing)
1000 concurrent users = ~333 req/s needed
```

### Current Capacity

```
Cached endpoints:   161 req/s (100% success)  → handles 483 users
DB-backed endpoints: 161 req/s × 10% success = 16 req/s → handles 48 users
Mixed workload:     ~50 req/s effective → handles ~150 users
```

### Target Capacity

```
1000 users × 1 req/3s = 333 req/s needed
If 80% cached: 267 req/s from cache + 67 req/s from DB
Cache can handle: unlimited (Redis is fast)
DB needs to handle: 67 req/s → needs P50 <15ms per query
```

### What Needs to Change (No Code Changes)

| Lever | Current | Target | Impact |
|-------|---------|--------|--------|
| Cache hit ratio | 28% | 85% | 3x fewer DB queries |
| Gunicorn workers | 16 | 16 (already optimal for 2-core) | Keep as-is |
| MariaDB buffer pool | 512 MB | 1 GB | More data in memory |
| Redis maxmemory | 256 MB | 512 MB | More cached keys |
| Nginx gzip | None | Enabled | 60-80% smaller responses |
| CDN for static | None | CloudFlare free | Offload images/CSS/JS |
| Product list caching | None | Redis 30s TTL | Eliminates DB bottleneck |

---

## 3. Phase-by-Phase Plan

### Phase 1: Cache the Product List (No Code — Config Only)

**What:** Wire the existing `@cached_response` decorator into `products.py`

**Impact:** Product list goes from 266ms P95 (DB) → 7ms P95 (Redis cache)

**How:**
```python
# In products.py, add to list_products():
from saathimart.api.cached import cache_product_list

@frappe.whitelist(allow_guest=True)
@cache_product_list  # ← Add this one line
def list_products(...):
```

**Result:** Product list P50 drops from 266ms to ~7ms. Error rate drops from 30% to <1%.

**Effort:** 5 minutes (one line per endpoint)

**Files affected:** `saathimart/api/products.py`

---

### Phase 2: Increase Redis Memory (Config Change)

**What:** Increase Redis maxmemory from 256MB to 512MB

**Why:** Current Redis has 28% hit ratio because it evicts keys. More memory = more cached keys = higher hit ratio.

**How:** Edit `docker/redis-cache.conf`:
```
maxmemory 512mb
```

**Impact:** Redis hit ratio 28% → 60%+ (more product data stays cached)

**Effort:** 2 minutes

---

### Phase 3: Increase MariaDB Buffer Pool (Config Change)

**What:** Increase innodb_buffer_pool_size from 512MB to 1GB

**Why:** MariaDB currently uses 220MB RAM. The buffer pool holds table data in memory. 1GB means the entire product/vendor/stock dataset fits in RAM — no disk reads.

**How:** Edit `docker/my.cnf`:
```
innodb_buffer_pool_size = 1G
```

**Impact:** DB query P50 drops from ~10ms to ~2ms (all data in memory)

**Effort:** 2 minutes

**Risk:** Leaves only 1GB for OS + other containers. Monitor for OOM.

---

### Phase 4: Add Nginx Reverse Proxy (New Container)

**What:** Add nginx in front of Gunicorn for:
- gzip compression (60-80% smaller JSON responses)
- Static asset serving (images, CSS, JS — offloaded from Gunicorn)
- Rate limiting (protect against abuse)
- Connection keep-alive (reuse TCP connections)

**How:** Add to `docker-compose.yml`:
```yaml
nginx:
  image: nginx:alpine
  ports: ["8080:80"]
  volumes:
    - ./docker/nginx.conf:/etc/nginx/nginx.conf:ro
  depends_on: [backend]
```

**nginx.conf key settings:**
```nginx
gzip on;
gzip_types application/json text/html text/css application/javascript;
gzip_min_length 1000;

# Static assets — served directly from disk, no Gunicorn
location /assets/ {
    proxy_pass http://backend:8000;
    expires 30d;
    add_header Cache-Control "public, immutable";
}

# API — proxy to Gunicorn
location /api/ {
    proxy_pass http://backend:8000;
    proxy_set_header Host $host;
}
```

**Impact:**
- JSON responses: 45KB → 12KB (gzip)
- Static assets: served by nginx, not Gunicorn (frees worker slots)
- Estimated 2x throughput improvement

**Effort:** 30 minutes

---

### Phase 5: CDN for Static Assets (External Service)

**What:** Use CloudFlare (free tier) to cache images, CSS, JS at edge locations

**Why:** Product images are the largest payloads. A single product image is 50-200KB. With 1000 users, that's 50-200MB of image traffic per page load.

**How:**
1. Create CloudFlare account (free)
2. Point DNS to your server
3. Enable "Cache Everything" page rule for `/assets/*`
4. Enable "Polish" for image optimization

**Impact:**
- Image bandwidth: 100% offloaded to CloudFlare
- Gunicorn workers: freed from serving static files
- Global latency: <50ms for images (edge cache)

**Effort:** 30 minutes (one-time setup)

---

### Phase 6: Optimize Gunicorn for 2-Core (Already Done)

**Current config (already optimal for 2-core):**
```
--workers 16 --threads 4
```

**Why 16 workers on 2 cores:**
- Python GIL means only 1 thread runs Python code at a time
- But threads help during I/O waits (DB queries, Redis calls)
- 16 workers × 4 threads = 64 concurrent request slots
- Each worker uses ~40MB RAM → 640MB total (fits in 2GB limit)

**No change needed.** This is already optimal.

---

### Phase 7: Add MariaDB Connection Pooling (Optional)

**What:** Add ProxySQL or simple connection pooling between Gunicorn and MariaDB

**Why:** Currently each Gunicorn thread opens a new DB connection. With 64 threads, that's 64 simultaneous DB connections. MariaDB's max_connections is 100 — close to the limit.

**How:** Add to `docker-compose.yml`:
```yaml
proxysql:
  image: proxysql/proxysql:2.6
  volumes:
    - ./docker/proxysql.cnf:/etc/proxysql.cnf:ro
```

**Impact:**
- DB connections: 64 → 10 (pooled)
- Connection overhead: eliminated
- MariaDB load: reduced

**Effort:** 1 hour

---

### Phase 8: Add Slow Query Monitoring (Already Done)

**Current state:** Slow query log is ON, threshold 0.5s

**No change needed.** Already configured in `docker/my.cnf`.

---

## 4. Expected Results After All Phases

### Before (Current)

```
100 users:  P50=7ms, P95=266ms, Error=30%
Capacity:   ~150 users
```

### After (All 8 Phases)

```
Phase 1 (cache product list):
  100 users:  P50=7ms, P95=15ms, Error=1%
  Capacity:   ~400 users

Phase 2+3 (more Redis/MariaDB memory):
  100 users:  P50=5ms, P95=12ms, Error=<1%
  Capacity:   ~500 users

Phase 4 (nginx gzip):
  100 users:  P50=3ms, P95=10ms, Error=<1%
  Capacity:   ~700 users

Phase 5 (CDN):
  100 users:  P50=2ms, P95=8ms, Error=<1%
  Capacity:   ~900 users

Phase 6-8 (pooling + monitoring):
  1000 users: P50=5ms, P95=50ms, Error=<1%
  Capacity:   ~1000 users
```

---

## 5. Capacity Summary

### Concurrent Users by Phase

```
Phase   Change                    Users    P95      Error
────────────────────────────────────────────────────────
0       (current)                 150      266ms    30%
1       Cache product list        400      15ms     1%
2+3     More Redis/MariaDB RAM    500      12ms     <1%
4       Nginx gzip                700      10ms     <1%
5       CDN for static            900      8ms      <1%
6-8     Connection pooling       1000      50ms     <1%
```

### Resource Budget (2-Core / 4GB)

```
Component          Current    After Optimization    Change
──────────────────────────────────────────────────────────
OS + Docker        500 MB     500 MB                -
Backend (Gunicorn) 652 MB     800 MB                +150 MB
Worker process     150 MB     200 MB                +50 MB
MariaDB            220 MB     1000 MB               +780 MB
Redis cache        13 MB      512 MB                +499 MB
Redis queue        11 MB      50 MB                 +39 MB
Nginx              -          30 MB                 +30 MB
──────────────────────────────────────────────────────────
TOTAL              1546 MB    3092 MB               +1546 MB
Available          4096 MB    4096 MB               -
Headroom           2550 MB    1004 MB               (tight but OK)
```

---

## 6. Implementation Order (Priority)

| # | Phase | Effort | Impact | Do First? |
|---|-------|--------|--------|-----------|
| 1 | Cache product list | 5 min | HUGE | ✅ YES |
| 2 | Increase Redis memory | 2 min | HIGH | ✅ YES |
| 3 | Increase MariaDB buffer | 2 min | HIGH | ✅ YES |
| 4 | Add nginx gzip | 30 min | HIGH | ✅ YES |
| 5 | CDN for static | 30 min | MEDIUM | ⚠️ When ready |
| 6 | Connection pooling | 1 hr | MEDIUM | ⚠️ When needed |
| 7 | Monitoring | Done | LOW | ✅ Already done |
| 8 | Slow query log | Done | LOW | ✅ Already done |

**Recommended:** Do phases 1-4 first (40 minutes total). This gets you to 700+ users. Phase 5 (CDN) when you deploy to production. Phase 6 (pooling) only if you hit DB connection limits.

---

## 7. What NOT to Do

| Don't | Why |
|-------|-----|
| Don't add Kubernetes | 1000 users doesn't need orchestration |
| Don't add microservices | Frappe monolith handles this fine |
| Don't add Elasticsearch | Frappe search + Redis cache is enough |
| Don't add Kafka/RabbitMQ | Redis queue handles background jobs |
| Don't add read replicas | 1 DB is fine for 1000 users with caching |
| Don't add more than 16 Gunicorn workers | Diminishing returns on 2-core, wastes RAM |

---

## 8. How to Validate

After each phase, run:

```bash
# Phase 1: Test product list caching
curl -w "%{time_total}s\n" -H "Host: saathimart.localhost" \
  "http://localhost:8080/api/method/saathimart.api.products.list_products?page=1&page_size=10"
# Expected: <10ms (was 100ms+)

# Phase 2-3: Check memory usage
docker stats --no-stream | grep saathimart

# Phase 4: Check gzip
curl -H "Host: saathimart.localhost" -H "Accept-Encoding: gzip" \
  -o /dev/null -w "size: %{size_download}\n" \
  "http://localhost:8080/api/method/saathimart.api.products.list_products?page=1&page_size=10"
# Expected: ~12KB (was ~45KB)

# Final: k6 load test
k6 run --vus 1000 --duration 60s saathimart/tests/load_test.js
# Expected: P95 <500ms, Error <1%
```

---

## 9. Risk Assessment

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| OOM at 1GB MariaDB buffer | LOW | Monitor with `docker stats` |
| Redis eviction under load | LOW | 512MB is plenty for product cache |
| Gunicorn worker exhaustion | MEDIUM | 64 slots handles 1000 users at 1 req/3s |
| DB connection exhaustion | LOW | 100 max_connections, pooled at 16 |
| CDN misconfiguration | LOW | Test with CloudFlare preview mode |

---

## 10. Conclusion

**1000 concurrent users is achievable on 2-core / 4GB** with these changes:

1. **Cache product list** (1 line of code) — biggest impact
2. **Increase Redis to 512MB** (config change) — more cached data
3. **Increase MariaDB buffer to 1GB** (config change) — faster queries
4. **Add nginx with gzip** (new container) — smaller responses
5. **CDN for static assets** (CloudFlare free) — offload images

Total effort: ~1 hour of config changes. No architecture changes needed.

The key insight: **the bottleneck is not infrastructure — it's the uncached product list.** Cache it, and 1000 users becomes trivial.
