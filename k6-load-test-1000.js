import http from 'k6/http';
import { check, sleep } from 'k6';

// Configuration for 1000 concurrent users on 2-core / 4GB server
export const options = {
  scenarios: {
    // Scenario 1: Product browsing (80% of traffic)
    productBrowsing: {
      executor: 'constant-vus',
      vus: 800,
      duration: '60s',
      exec: 'testProductBrowsing',
      tags: { scenario: 'product_browsing' },
    },
    // Scenario 2: Cart operations (10% of traffic)
    cartOperations: {
      executor: 'constant-vus',
      vus: 100,
      duration: '60s',
      exec: 'testCartOperations',
      tags: { scenario: 'cart_operations' },
    },
    // Scenario 3: Search operations (5% of traffic)
    searchOperations: {
      executor: 'constant-vus',
      vus: 50,
      duration: '60s',
      exec: 'testSearch',
      tags: { scenario: 'search' },
    },
    // Scenario 4: Health checks (5% of traffic)
    healthChecks: {
      executor: 'constant-vus',
      vus: 50,
      duration: '60s',
      exec: 'testHealthCheck',
      tags: { scenario: 'health' },
    },
  },
  thresholds: {
    http_req_duration: ['p(95)<500'],
    http_req_failed: ['rate<0.01'],
  },
};

const BASE_URL = 'http://localhost:8080';
const HOST_HEADER = { 'Host': 'saathimart.localhost' };

// ─── Product Browsing Scenario (80% of traffic) ──────────────────────────────
export function testProductBrowsing() {
  // Browse products page
  const res = http.get(`${BASE_URL}/api/method/saathimart.api.products.list_products?page=1&page_size=20`, HOST_HEADER);
  
  check(res, {
    'products list status 200': (r) => r.status === 200,
    'products list response time < 500ms': (r) => r.timings.duration < 500,
  });

  // Randomly filter by category (20% of requests)
  if (Math.random() < 0.2) {
    http.get(`${BASE_URL}/api/method/saathimart.api.products.list_products?category=food&page=1&page_size=20`, HOST_HEADER);
  }

  // Randomly filter by brand (15% of requests)
  if (Math.random() < 0.15) {
    http.get(`${BASE_URL}/api/method/saathimart.api.products.list_products?brand=nestle&page=1&page_size=20`, HOST_HEADER);
  }

  sleep(1 + Math.random());
}

// ─── Cart Operations Scenario (10% of traffic) ───────────────────────────────
export function testCartOperations() {
  // Get cart summary
  const res = http.get(`${BASE_URL}/api/method/saathimart.api.cart.get_cart_summary`, HOST_HEADER);
  
  check(res, {
    'cart summary status 200': (r) => r.status === 200,
  });

  // Get cart count for badge
  http.get(`${BASE_URL}/api/method/saathimart.api.cart.get_cart_count`, HOST_HEADER);

  sleep(2 + Math.random() * 2);
}

// ─── Search Scenario (5% of traffic) ─────────────────────────────────────────
export function testSearch() {
  // Basic search
  http.get(`${BASE_URL}/api/method/saathimart.api.search.search_products?query=milk&page=1&page_size=20`, HOST_HEADER);

  // Search with category filter
  http.get(`${BASE_URL}/api/method/saathimart.api.search.search_products?query=rice&category=food&page=1&page_size=20`, HOST_HEADER);

  // Search suggestions
  http.get(`${BASE_URL}/api/method/saathimart.api.search.search_suggestions?query=mi&limit=8`, HOST_HEADER);

  sleep(3 + Math.random() * 2);
}

// ─── Health Check Scenario (5% of traffic) ───────────────────────────────────
export function testHealthCheck() {
  const res = http.get(`${BASE_URL}/api/method/saathimart.api.health.health_check`, HOST_HEADER);
  
  check(res, {
    'health check status 200': (r) => r.status === 200,
    'health check response time < 50ms': (r) => r.timings.duration < 50,
  });

  sleep(5);
}
