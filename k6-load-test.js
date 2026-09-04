import http from 'k6/http';
import { sleep } from 'k6';

export const options = {
  vus: 1000,
  duration: '120s',
  thresholds: {
    http_req_duration: ['p(95)<500'],
    http_req_failed: ['rate<0.01'],
  },
};

export default function() {
  const res = http.get('http://localhost:8080/api/method/saathimart.api.products.list_products?page=1&page_size=10', {
    headers: { 'Host': 'saathimart.localhost' },
  });
  
  if (res.status !== 200) {
    console.log(`ERROR: Status ${res.status}`);
  }
  
  // Users take ~2 seconds between actions (browsing)
  sleep(2);
}
