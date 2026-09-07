# SaathiMart K6 Load Testing

## Overview

This directory contains comprehensive k6 load tests for all SaathiMart APIs, designed to verify system performance under 1000 concurrent users.

## Test Files

### 1. `k6-critical-apis.js` (Recommended for First Run)
Tests the most critical API endpoints that users interact with:
- Product browsing and search
- Cart operations
- Checkout flow
- Homepage and CMS
- Delivery and location services
- Loyalty and coupons

**Duration:** ~6 minutes  
**Peak Load:** 1000 concurrent users

### 2. `k6-comprehensive-test.js` (Full Coverage)
Tests all 90+ API endpoints across both saathimart and saathimart-vendor apps:
- Authentication (signup, login, OTP)
- Products & Search
- Cart operations
- Checkout & Orders
- Payments
- CMS & Content
- Location & Delivery
- Loyalty & Coupons
- Reviews & Wishlist
- Vendor APIs

**Duration:** ~5 minutes  
**Peak Load:** 1000 concurrent users

## Prerequisites

### Option 1: Local k6 Installation
```bash
# macOS
brew install k6

# Verify installation
k6 version
```

### Option 2: Docker-based k6 (No Installation Required)
```bash
# Pull k6 Docker image
docker pull grafana/k6:latest
```

## Running the Tests

### Step 1: Start Docker Containers

```bash
cd /Users/ashutoshneupane/Desktop/School_saas/saathimart

# Start all containers
docker-compose up -d

# Wait for containers to be healthy (60 seconds)
sleep 60

# Verify containers are running
docker ps
```

### Step 2: Verify API Connectivity

```bash
# Quick API test
chmod +x quick-api-test.sh
./quick-api-test.sh
```

Expected output:
```
List Products.....................................✓ OK (200)
Get Cart..........................................✓ OK (200)
Get Payment Modes..................................✓ OK (200)
...
```

### Step 3: Run Load Test

#### Option A: Using Local k6
```bash
chmod +x run-load-test.sh
./run-load-test.sh
```

#### Option B: Using Docker-based k6
```bash
chmod +x run-load-test-docker.sh
./run-load-test-docker.sh
```

#### Option C: Manual k6 Run
```bash
# Run critical APIs test
k6 run k6-critical-apis.js

# Run comprehensive test
k6 run k6-comprehensive-test.js

# Run with custom parameters
k6 run \
  --vus 500 \
  --duration 2m \
  --out json=results.json \
  k6-critical-apis.js
```

## Test Configuration

### Environment Variables

```bash
# Base URL (default: http://localhost:8080)
export BASE_URL="http://your-server:8080"

# Hub host (default: saathimart.localhost)
export HUB_HOST="saathimart.localhost"

# Vendor host (default: vendor.localhost)
export VENDOR_HOST="vendor.localhost"
```

### Customizing Load

Edit the `options` section in the test files:

```javascript
export const options = {
  stages: [
    { duration: '30s', target: 100 },   // Ramp up to 100
    { duration: '30s', target: 500 },   // Ramp up to 500
    { duration: '1m', target: 1000 },   // Ramp up to 1000
    { duration: '3m', target: 1000 },   // Stay at 1000
    { duration: '30s', target: 0 },     // Ramp down
  ],
  thresholds: {
    http_req_duration: ['p(95)<500'],  // 95% under 500ms
    http_req_failed: ['rate<0.05'],    // <5% errors
  },
};
```

## Understanding Results

### Key Metrics

1. **http_reqs** - Total number of HTTP requests made
2. **http_req_duration** - Request latency
   - `p(95)` - 95% of requests faster than this
   - `p(99)` - 99% of requests faster than this
3. **http_req_failed** - Error rate (should be < 5%)
4. **errors** - Custom error rate from business logic
5. **iterations** - Number of complete test cycles

### Sample Output

```
     ✗ status is 200
      ↳  98% — ✓ 15234 / ✗ 312

   ✓[==================100%] 1000 VUs

     data_received..............: 45 MB
     data_sent..................: 12 MB
     http_req_duration..........: avg=120ms min=45ms med=95ms max=2.1s p(95)=280ms p(99)=450ms
     http_req_failed............: 2.3%
     http_reqs..................: 15546
     iterations.................: 12000
```

### Interpreting Results

✅ **Good Results:**
- `p(95) < 500ms` - 95% of requests under 500ms
- `http_req_failed < 5%` - Less than 5% errors
- `errors < 10%` - Less than 10% business logic errors

⚠️ **Warning Signs:**
- `p(95) > 500ms` - API is slow under load
- `http_req_failed > 5%` - Too many HTTP errors
- High error rates in specific categories (auth_errors, cart_errors, etc.)

❌ **Critical Issues:**
- `p(95) > 1000ms` - API is very slow
- `http_req_failed > 10%` - System is unstable
- 500 errors - Server crashes or database issues

## Common Errors and Fixes

### 1. Connection Refused
```
FAIL: GET saathimart.api.products.list_products - 000
```
**Fix:** Containers are not running or not ready
```bash
docker-compose up -d
sleep 60
docker ps
```

### 2. High Latency (p(95) > 500ms)
**Causes:**
- Database not indexed
- Insufficient worker processes
- Redis cache not configured
- Large payload sizes

**Fixes:**
```bash
# Check indexes
bench --site saathimart.localhost execute saathimart.api.indexes.add_performance_indexes

# Increase workers (in docker-compose.yml)
WORKERS: 32
THREADS: 4

# Check Redis
docker exec -it saathimart-redis-cache redis-cli INFO memory
```

### 3. High Error Rate (> 5%)
**Causes:**
- Database connection pool exhausted
- Rate limiting triggered
- Validation errors
- Missing test data

**Fixes:**
```bash
# Check logs
docker logs saathimart-backend --tail 100

# Check database connections
docker exec -it saathimart-mariadb mysql -uroot -proot -e "SHOW PROCESSLIST;"

# Seed test data
bench --site saathimart.localhost execute saathimart.scripts.seed.run
```

### 4. Out of Memory
**Causes:**
- Too many concurrent workers
- Memory leak in code
- Large result sets

**Fixes:**
```yaml
# In docker-compose.yml, increase memory limits
deploy:
  resources:
    limits:
      memory: 8192M
```

### 5. Rate Limiting
```
ERROR: Too many requests
```
**Fix:** Adjust rate limits in the code or run test with fewer VUs

## Performance Benchmarks

### Expected Performance (1000 VUs)

| API Category | Expected p(95) | Max Error Rate |
|-------------|----------------|----------------|
| Products | < 200ms | < 2% |
| Cart | < 300ms | < 5% |
| Checkout | < 500ms | < 5% |
| CMS | < 150ms | < 1% |
| Search | < 250ms | < 3% |

### System Requirements

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| CPU | 4 cores | 8+ cores |
| RAM | 8 GB | 16+ GB |
| Database | MariaDB 10.6 | MariaDB 11+ |
| Redis | 256 MB | 512+ MB |
| Workers | 8 | 32 |

## Monitoring During Test

### Real-time Metrics

```bash
# Watch container stats
watch -n 1 'docker stats --no-stream'

# Monitor Redis
watch -n 1 'docker exec saathimart-redis-cache redis-cli --stat'

# Monitor database
watch -n 5 'docker exec saathimart-mariadb mysql -uroot -proot -e "SHOW STATUS LIKE '\''Threads_connected'\'';"'
```

### Logs

```bash
# Backend logs
docker logs -f saathimart-backend

# Nginx logs
docker logs -f saathimart-nginx

# All containers
docker-compose logs -f
```

## Test Data Setup

### Seed Test Products

```bash
bench --site saathimart.localhost execute saathimart.scripts.seed.run
```

### Create Test Vendors

```bash
bench --site saathimart.localhost console
>>> import frappe
>>> # Create test vendors...
```

## Troubleshooting Checklist

- [ ] Docker containers are running
- [ ] All containers show "healthy" status
- [ ] API responds to simple GET request
- [ ] Database has test data
- [ ] Redis is accessible
- [ ] No errors in container logs
- [ ] Sufficient system resources (CPU, RAM)
- [ ] Network connectivity between containers

## Advanced Usage

### Run Specific Scenario

```javascript
// Comment out other scenarios in the test file
if (Math.random() < 1.0) {  // Always run this scenario
  group('Browse Products', () => {
    // ...
  });
}
```

### Custom Metrics

```javascript
import { Counter } from 'k6/metrics';

const myMetric = new Counter('my_custom_metric');

// In your test
myMetric.add(1);
```

### Export to InfluxDB/Grafana

```bash
k6 run --out influxdb=http://localhost:8086/k6 k6-critical-apis.js
```

## Support

For issues or questions:
1. Check container logs: `docker logs saathimart-backend`
2. Review error messages in test output
3. Verify database and Redis connectivity
4. Check system resources: `docker stats`

## Files

- `k6-critical-apis.js` - Core API test (recommended)
- `k6-comprehensive-test.js` - Full coverage test
- `quick-api-test.sh` - Quick connectivity verification
- `run-load-test.sh` - Automated test runner
- `run-load-test-docker.sh` - Docker-based test runner
