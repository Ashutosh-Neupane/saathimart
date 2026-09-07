import http from "k6/http";
import { check, sleep } from "k6";
import { Counter, Rate, Trend } from "k6/metrics";

// ── 100k-order soak: 10 waves × 1000 concurrent users, 10 orders each ──────
// Each user adds 1 item to cart then checks out. Wave boundaries are marked
// with the custom WAVE metric so per-wave pace can be charted.

const API_KEY = __ENV.API_KEY;
const API_SECRET = __ENV.API_SECRET;
const BASE = "http://localhost:8000";
const HOST = "saathimart.localhost";
const PRODUCT = __ENV.PRODUCT || "Load Test Rice 25kg";
const ZONE = __ENV.ZONE || "Checkout Test Zone";
const VUS = 1000;
const ORDERS_PER_VU = 10;

const orders = new Counter("orders_created");
const checkout_fails = new Rate("checkout_fails");
const cart_fails = new Rate("cart_fails");
const wave_mark = new Trend("wave_time_ms");

const params = {
  headers: {
    Host: HOST,
    "Content-Type": "application/json",
    Authorization: `token ${API_KEY}:${API_SECRET}`,
  },
  timeout: "120s",
};

export const options = {
  scenarios: {
    soak: {
      executor: "per-vu-iterations",
      vus: VUS,
      iterations: ORDERS_PER_VU,
      maxDuration: "2h",
      gracefulStop: "60s",
    },
  },
  thresholds: {
    // report-only thresholds: we want to MEASURE failure, not abort the run
    checkout_fails: [{ threshold: "rate<1", abortOnFail: false }],
  },
  discardResponseBodies: true,
};

let waveNum = 0;
let waveStart = Date.now();
let ordersInWave = 0;

function markWave() {
  ordersInWave++;
  if (ordersInWave >= VUS) {
    waveNum++;
    wave_mark.add(Date.now() - waveStart);
    console.log(`WAVE ${waveNum} done in ${((Date.now() - waveStart) / 1000).toFixed(1)}s`);
    waveStart = Date.now();
    ordersInWave = 0;
  }
}

export default function () {
  const session = `LOAD-${__VU}-${__ITER}`;

  // 1) add to cart
  const cart = http.post(
    `${BASE}/api/method/saathimart.api.cart.add_to_cart`,
    JSON.stringify({ product: PRODUCT, qty: 1 }),
    params
  );
  const cartOk = check(cart, { "cart 2xx": (r) => r.status === 200 });
  if (!cartOk) {
    cart_fails.add(1);
    sleep(0.5);
    return;
  }

  // 2) checkout
  const co = http.post(
    `${BASE}/api/method/saathimart.api.orders.checkout`,
    JSON.stringify({
      session_id: session,
      customer_name: `Load User ${__VU}`,
      customer_phone: `98${String(__VU).padStart(8, "0")}`,
      delivery_address: `${__VU} Test Street`,
      delivery_zone: ZONE,
      payment_method: "COD",
    }),
    params
  );
  const coOk = check(co, {
    "checkout 2xx": (r) => r.status === 200,
  });
  if (coOk) {
    orders.add(1);
  } else {
    checkout_fails.add(1);
    if (__ITER < 3) {
      console.warn(`VU ${__VU} iter ${__ITER}: checkout ${co.status}`);
    }
  }
  markWave();

  // tiny think time so cart/checkout look human-ish
  sleep(Math.random() * 0.4);
}
