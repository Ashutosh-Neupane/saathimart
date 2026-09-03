import http from 'k6/http';
import { sleep } from 'k6';

export const options = {
  scenarios: {
    webTraffic: {
      executor: 'constant-vus',
      vus: 100,
      duration: '30s',
      exec: 'testPing',
      tags: { test_type: 'web_traffic' },
    },
  },
  thresholds: {
    http_req_duration: ['p(95)<2000'],
    http_req_failed: ['rate<0.10'],
  },
};

export function testPing() {
  const res = http.get('http://localhost:55233/api/method/ping', {
    headers: { 'Host': 'saathimart.localhost' },
  });
  
  console.log(`Status: ${res.status}`);
  console.log(`Response: ${res.body}`);
  
  sleep(1);
}
