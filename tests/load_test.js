import http from 'k6/http';
import { check, sleep } from 'k6';
import { Rate, Trend } from 'k6/metrics';

const errorRate = new Rate('errors');
const BASE = __ENV.BASE_URL || 'http://127.0.0.1:8000';
const H = { 'Host': 'saathimart.localhost' };

export const options = {
  stages: [
    { duration: '30s', target: 100 },   // Ramp to 100
    { duration: '60s', target: 100 },   // Sustain 100
    { duration: '10s', target: 0 },     // Ramp down
  ],
  thresholds: {
    http_req_duration: ['p(50)<50', 'p(95)<200', 'p(99)<500'],
    errors: ['rate<0.05'],
  },
};

export default function () {
  // Product list (most hit)
  let res = http.get(`${BASE}/api/method/saathimart.api.products.list_products?page=1&page_size=10`, { headers: H, timeout: '10s' });
  check(res, { 'product list 200': (r) => r.status === 200 }) || errorRate.add(1);
  sleep(0.5);

  // Health
  res = http.get(`${BASE}/api/method/saathimart.api.health.health_check`, { headers: H, timeout: '5s' });
  check(res, { 'health 200': (r) => r.status === 200 }) || errorRate.add(1);
  sleep(0.3);

  // Categories
  res = http.get(`${BASE}/api/method/saathimart.api.products.list_categories`, { headers: H, timeout: '5s' });
  check(res, { 'categories 200': (r) => r.status === 200 }) || errorRate.add(1);
  sleep(0.3);

  // Search
  res = http.get(`${BASE}/api/method/saathimart.api.search.search_products?q=milk`, { headers: H, timeout: '10s' });
  check(res, { 'search 200': (r) => r.status === 200 }) || errorRate.add(1);
  sleep(0.5);
}
