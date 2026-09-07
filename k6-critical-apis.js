/**
 * K6 Load Test - Critical SaathiMart APIs
 * Focus: Products, Cart, Checkout, Orders
 * Load: 1000 concurrent users
 * 
 * Run: k6 run k6-critical-apis.js
 * Or:  docker run --rm -i grafana/k6 run - < k6-critical-apis.js
 */

import http from 'k6/http';
import { sleep, check, group } from 'k6';
import { Rate, Trend, Counter } from 'k6/metrics';

// ── Configuration ─────────────────────────────────────────────────────────────

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8000';
const HUB_HOST = __ENV.HUB_HOST || 'saathimart.localhost';

// ── Real product+vendor pairs from the DB ─────────────────────────────────────
// These are the only valid combinations — random pairing causes "Vendor listing not found"
const PRODUCT_VENDOR_PAIRS = [
  { product: 'Test Tomato',              vendor: 'c8dbs98jph' },
  { product: 'Cart Test Product',        vendor: 'c7jfpppeqc' },
  { product: 'Checkout Product',         vendor: 'c7pu1obvkr' },
  { product: 'Checkout Vendor Product',  vendor: 'c7tqr9btld' },
  { product: 'Checkout Vendor Product',  vendor: 'c7tm1dbeig' },
  { product: 'Checkout Vendor Product B', vendor: 'c7u2djqnbd' },
  { product: 'Checkout Vendor Product C', vendor: 'c7v97bgvta' },
  { product: 'Status Test Product',      vendor: 'c8b7586hqf' },
  { product: 'Stock Serialization Product', vendor: 'c8edtbj6al' },
  { product: 'Variant Test Plain Product', vendor: 'c8h8k557u6' },
  { product: 'SLE Test Product',         vendor: 'c8ii7bmrj9' },
  { product: 'Totals Product A',         vendor: 'c8j8dcubnk' },
  { product: 'Totals Product B',         vendor: 'c8jf89suj4' },
  { product: 'Resolver Plain Product',   vendor: 'c8np60k46g' },
  { product: 'Resolver Tee Blue M',      vendor: 'c8l8pdumnk' },
  { product: 'Resolver Tee Red L',       vendor: 'c8ln328jh0' },
  { product: 'Nearest Test Product',     vendor: 'vendor1.localhost' },
  { product: 'Nearest Test Product',     vendor: 'vendor2.localhost' },
  { product: 'Event Test Product',       vendor: 'c946mqvt75' },
  { product: 'Stock Test Product',       vendor: 'c93lg5prcv' },
];

// Custom metrics
const errorRate = new Rate('errors');
const apiLatency = new Trend('api_latency');

// ── Load Test Options ────────────────────────────────────────────────────────

export const options = {
  stages: [
    { duration: '30s', target: 100 },   // Ramp up to 100
    { duration: '30s', target: 500 },   // Ramp up to 500
    { duration: '1m', target: 1000 },   // Ramp up to 1000
    { duration: '3m', target: 1000 },   // Stay at 1000
    { duration: '30s', target: 0 },     // Ramp down
  ],
  thresholds: {
    http_req_duration: ['p(95)<500', 'p(99)<1000'],
    http_req_failed: ['rate<0.05'],
    errors: ['rate<0.1'],
  },
};

// ── Helper Functions ──────────────────────────────────────────────────────────

function apiGet(path, params = {}) {
  const url = `${BASE_URL}/api/method/${path}`;
  const queryString = Object.keys(params).map(k => `${k}=${encodeURIComponent(params[k])}`).join('&');
  const fullUrl = queryString ? `${url}?${queryString}` : url;

  // Each VU gets a unique simulated IP so per-IP rate limits work correctly.
  // In production, real users come from different IPs — this mirrors that.
  const simulatedIP = `10.${Math.floor(__VU / 254)}.${__VU % 254}.${(__ITER % 254) + 1}`;
  
  const res = http.get(fullUrl, {
    headers: {
      'Host': HUB_HOST,
      'X-Forwarded-For': simulatedIP,
      'X-Real-IP': simulatedIP,
    },
    timeout: '30s',
  });
  
  apiLatency.add(res.timings.duration);
  const ok = res.status === 200;
  errorRate.add(!ok);
  if (!ok) {
    console.log(`FAIL: GET ${path} - ${res.status} - ${res.body.substring(0, 100)}`);
  }
  return res;
}

function apiPost(path, payload) {
  const url = `${BASE_URL}/api/method/${path}`;
  const simulatedIP = `10.${Math.floor(__VU / 254)}.${__VU % 254}.${(__ITER % 254) + 1}`;

  const res = http.post(url, JSON.stringify(payload), {
    headers: {
      'Host': HUB_HOST,
      'Content-Type': 'application/json',
      'X-Forwarded-For': simulatedIP,
      'X-Real-IP': simulatedIP,
    },
    timeout: '30s',
  });
  
  apiLatency.add(res.timings.duration);
  // 400 = validation error (expected for empty carts, etc.) — not a system failure
  const ok = res.status === 200 || res.status === 400;
  errorRate.add(!ok);
  if (!ok) {
    console.log(`FAIL: POST ${path} - ${res.status} - ${res.body.substring(0, 100)}`);
  }
  return res;
}

// ── Test Flow ─────────────────────────────────────────────────────────────────

export default function() {
  const sessionId = `session_${__VU}_${__ITER}`;
  // Use real product+vendor pairs from DB to avoid 404s
  const pair = PRODUCT_VENDOR_PAIRS[Math.floor(Math.random() * PRODUCT_VENDOR_PAIRS.length)];

  // ── Scenario 1: Browse Products (Most Common) ───────────────────────────────
  group('Browse Products', () => {
    const r1 = apiGet('saathimart.api.products.list_products', { page: '1', page_size: '20' });
    check(r1, { 'products list 200': (r) => r.status === 200 });
    sleep(1);

    // Search is rate-limited to 30/min per-IP — only 20% of VUs hit it per iteration
    if (Math.random() < 0.2) {
      const r2 = apiGet('saathimart.api.search.search_products', { query: 'tomato', page: '1' });
      check(r2, { 'search 200': (r) => r.status === 200 });
      sleep(1);
    }

    const r3 = apiGet('saathimart.api.filters.get_filters');
    check(r3, { 'filters 200': (r) => r.status === 200 });
  });

  sleep(2);

  // ── Scenario 2: View Homepage ───────────────────────────────────────────────
  group('Homepage', () => {
    apiGet('saathimart.api.home.get_homepage_data');
    apiGet('saathimart.api.cms.get_site_config');
    apiGet('saathimart.api.home.get_banners');
    apiGet('saathimart.api.home.get_deals');
  });

  sleep(2);

  // ── Scenario 3: Cart Operations ─────────────────────────────────────────────
  group('Cart', () => {
    // Get cart
    apiGet(`saathimart.api.cart.get_cart`, { session_id: sessionId });
    sleep(0.5);
    
    // Add to cart - use real product+vendor pair from DB
    apiPost('saathimart.api.cart.add_to_cart', {
      session_id: sessionId,
      product: pair.product,
      qty: 1,
      vendor: pair.vendor,
    });
    sleep(0.5);
    
    // Get cart summary
    apiGet('saathimart.api.cart.get_cart_summary', { session_id: sessionId });
    sleep(0.5);
    
    // Get cart count
    apiGet('saathimart.api.cart.get_cart_count', { session_id: sessionId });
  });

  sleep(2);

  // ── Scenario 4: Checkout Preparation ────────────────────────────────────────
  group('Pre-Checkout', () => {
    // Get payment modes
    apiGet('saathimart.api.payments.get_payment_modes');
    sleep(0.5);
    
    // Get delivery zones
    apiGet('saathimart.api.delivery.get_delivery_zones');
    sleep(0.5);
    
    // Set customer location
    apiPost('saathimart.api.cart.set_customer_location', {
      session_id: sessionId,
      lat: 27.7172,
      lng: 85.3240,
    });
  });

  sleep(1);

  // ── Scenario 5: Checkout Attempt (10% of iterations) ────────────────────────
  if (__ITER % 10 === 0) {
    group('Checkout', () => {
      const phone = `98${String(Math.floor(Math.random() * 100000000)).padStart(8, '0')}`;
      
      apiPost('saathimart.api.orders.checkout', {
        session_id: sessionId,
        customer_name: `Test User ${__VU}`,
        customer_phone: phone,
        delivery_address: 'Test Address, Kathmandu',
        payment_method: 'COD',
        delivery_zone: 'Kathmandu',
      });
    });
  }

  // ── Scenario 6: Vendor Health Check ─────────────────────────────────────────
  if (__ITER % 20 === 0) {
    group('Vendor Check', () => {
      const vendorHost = Math.random() < 0.5 ? 'vendor1.localhost' : 'vendor2.localhost';
      const vendorHealth = http.get(`http://localhost:8001/api/method/saathimart_vendor.api.receive.health`, {
        headers: { 'Host': vendorHost },
      });
      check(vendorHealth, { 'vendor healthy': (r) => r.status === 200 });
    });
  }

  // User think time
  sleep(Math.random() * 3 + 1);
}

// ── Setup ─────────────────────────────────────────────────────────────────────

export function setup() {
  console.log('Starting SaathiMart Critical API Load Test');
  console.log(`Target: ${BASE_URL}`);
  console.log(`Host: ${HUB_HOST}`);
  
  // Verify connectivity
  const test = http.get(`${BASE_URL}/api/method/saathimart.api.products.list_products?page=1&page_size=1`, {
    headers: { 'Host': HUB_HOST },
  });
  
  if (test.status !== 200) {
    console.log(`WARNING: Initial test failed with status ${test.status}`);
    console.log(`Response: ${test.body}`);
  } else {
    console.log('✓ API connectivity verified');
  }
  
  return { start: Date.now() };
}

// ── Teardown ──────────────────────────────────────────────────────────────────

export function teardown(data) {
  console.log(`\nTest duration: ${((Date.now() - data.start) / 1000).toFixed(2)}s`);
}
