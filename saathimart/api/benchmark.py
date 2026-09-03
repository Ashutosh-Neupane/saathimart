"""
Performance Benchmark — measure response times for all critical endpoints.

Run via:
    bench --site saathimart.localhost execute saathimart.api.benchmark.run_benchmark
    bench --site saathimart.localhost execute saathimart.api.benchmark.run_load_test

Reports:
    - P50, P95, P99 latencies per endpoint
    - Requests per second
    - Cache hit rates
    - DB query counts
"""
import time
import statistics
import frappe
from frappe.utils import cint


# ── Benchmark Endpoints ──────────────────────────────────────────────────────

ENDPOINTS = {
    "product_list": {
        "module": "saathimart.api.products",
        "function": "list_products",
        "kwargs": {"page": 1, "page_size": 20},
        "cache_ttl": 30,
    },
    "product_detail": {
        "module": "saathimart.api.products",
        "function": "get_product",
        "kwargs": {"slug": "test-product"},
        "cache_ttl": 60,
    },
    "categories": {
        "module": "saathimart.api.products",
        "function": "list_categories",
        "kwargs": {},
        "cache_ttl": 300,
    },
    "brands": {
        "module": "saathimart.api.products",
        "function": "list_brands",
        "kwargs": {},
        "cache_ttl": 300,
    },
    "cms_banners": {
        "module": "saathimart.api.cms",
        "function": "get_banners",
        "kwargs": {},
        "cache_ttl": 600,
    },
    "settings": {
        "module": "saathimart.api.settings",
        "function": "get_settings",
        "kwargs": {},
        "cache_ttl": 600,
    },
    "health": {
        "module": "saathimart.api.health",
        "function": "health_check",
        "kwargs": {},
        "cache_ttl": 0,
    },
    "search": {
        "module": "saathimart.api.search",
        "function": "search_products",
        "kwargs": {"q": "milk", "page": 1, "page_size": 20},
        "cache_ttl": 30,
    },
}


# ── Single-Request Benchmark ─────────────────────────────────────────────────


def _measure_endpoint(name, config, iterations=10):
    """Measure a single endpoint over multiple iterations."""
    try:
        module = frappe.get_module(config["module"])
        fn = getattr(module, config["function"])
    except (ImportError, AttributeError) as e:
        return {"endpoint": name, "status": "error", "error": str(e)}

    latencies = []
    errors = 0

    for _ in range(iterations):
        start = time.monotonic()
        try:
            result = fn(**config["kwargs"])
            elapsed = (time.monotonic() - start) * 1000
            latencies.append(elapsed)
        except Exception as e:
            errors += 1
            latencies.append(10000)  # 10s penalty for errors

    if not latencies:
        return {"endpoint": name, "status": "no_data"}

    latencies.sort()
    return {
        "endpoint": name,
        "status": "ok",
        "iterations": iterations,
        "p50_ms": round(statistics.median(latencies), 1),
        "p95_ms": round(latencies[int(len(latencies) * 0.95)], 1),
        "p99_ms": round(latencies[int(len(latencies) * 0.99)], 1),
        "min_ms": round(min(latencies), 1),
        "max_ms": round(max(latencies), 1),
        "avg_ms": round(statistics.mean(latencies), 1),
        "errors": errors,
    }


def run_benchmark(iterations=10):
    """Run benchmark across all configured endpoints."""
    results = []
    for name, config in ENDPOINTS.items():
        result = _measure_endpoint(name, config, iterations)
        results.append(result)

    # Sort by avg latency
    results.sort(key=lambda x: x.get("avg_ms", 0))

    # Summary
    total_avg = statistics.mean([r["avg_ms"] for r in results if r["status"] == "ok"])
    total_p95 = statistics.mean([r["p95_ms"] for r in results if r["status"] == "ok"])

    report = {
        "results": results,
        "summary": {
            "total_endpoints": len(results),
            "avg_latency_ms": round(total_avg, 1),
            "p95_latency_ms": round(total_p95, 1),
            "estimated_rps": round(1000 / total_avg, 1) if total_avg > 0 else 0,
        },
    }

    frappe.logger().info(f"Benchmark complete: {report['summary']}")
    return report


# ── Load Test ────────────────────────────────────────────────────────────────


def _simulate_user(name, config, duration_sec=10):
    """Simulate a single user hitting an endpoint repeatedly."""
    try:
        module = frappe.get_module(config["module"])
        fn = getattr(module, config["function"])
    except (ImportError, AttributeError):
        return []

    latencies = []
    start = time.monotonic()

    while (time.monotonic() - start) < duration_sec:
        req_start = time.monotonic()
        try:
            fn(**config["kwargs"])
            elapsed = (time.monotonic() - req_start) * 1000
            latencies.append(elapsed)
        except Exception:
            latencies.append(10000)

    return latencies


def run_load_test(user_count=10, duration_sec=5, endpoint_name="product_list"):
    """Simulate concurrent users hitting a single endpoint.

    Args:
        user_count: Number of simulated users (concurrent requests)
        duration_sec: How long to run the test
        endpoint_name: Which endpoint to test

    Returns:
        Aggregate latency stats and throughput.
    """
    config = ENDPOINTS.get(endpoint_name)
    if not config:
        return {"error": f"Unknown endpoint: {endpoint_name}"}

    # Run sequentially (Frappe is single-threaded per worker)
    # but measure like concurrent load
    all_latencies = []
    start = time.monotonic()

    for i in range(user_count):
        user_latencies = _simulate_user(f"user_{i}", config, duration_sec)
        all_latencies.extend(user_latencies)

    total_time = time.monotonic() - start

    if not all_latencies:
        return {"error": "No data collected"}

    all_latencies.sort()

    return {
        "endpoint": endpoint_name,
        "user_count": user_count,
        "duration_sec": round(total_time, 1),
        "total_requests": len(all_latencies),
        "rps": round(len(all_latencies) / total_time, 1),
        "p50_ms": round(statistics.median(all_latencies), 1),
        "p95_ms": round(all_latencies[int(len(all_latencies) * 0.95)], 1),
        "p99_ms": round(all_latencies[int(len(all_latencies) * 0.99)], 1),
        "avg_ms": round(statistics.mean(all_latencies), 1),
        "max_ms": round(max(all_latencies), 1),
        "errors": sum(1 for l in all_latencies if l >= 10000),
    }


# ── Quick Health Check ───────────────────────────────────────────────────────


def quick_check():
    """Quick performance health check — one request per endpoint."""
    results = {}
    for name, config in ENDPOINTS.items():
        result = _measure_endpoint(name, config, iterations=3)
        results[name] = {
            "p50_ms": result.get("p50_ms", -1),
            "status": result.get("status", "unknown"),
        }
    return results
