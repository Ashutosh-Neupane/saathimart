#!/bin/bash
# SaathiMart K6 Load Test Runner
# Tests all critical APIs with 1000 concurrent users

set -e

echo "═══════════════════════════════════════════════════════════════════════"
echo "               SaathiMart K6 Load Test - 1000 Concurrent Users          "
echo "═══════════════════════════════════════════════════════════════════════"

# Configuration
COMPOSE_DIR="/Users/ashutoshneupane/Desktop/School_saas/saathimart"
TEST_FILE="${COMPOSE_DIR}/k6-critical-apis.js"
REPORT_DIR="${COMPOSE_DIR}/load-test-results"

# Create report directory
mkdir -p "${REPORT_DIR}"

# Check if Docker is running
echo ""
echo "Step 1: Checking Docker status..."
if ! docker ps &>/dev/null; then
    echo "❌ Docker daemon is not running. Please start Docker Desktop."
    echo "   Opening Docker Desktop..."
    open -a "Docker Desktop"
    echo "   Waiting 30 seconds for Docker to start..."
    sleep 30
fi

# Check if containers are running
echo ""
echo "Step 2: Checking Docker containers..."
cd "${COMPOSE_DIR}"

if ! docker ps | grep -q "saathimart-backend"; then
    echo "⚠️  SaathiMart containers are not running."
    echo ""
    read -p "Do you want to start them? (y/n) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo "Starting containers..."
        docker-compose up -d
        echo "Waiting 60 seconds for containers to be ready..."
        sleep 60
    else
        echo "Cannot run load test without containers."
        exit 1
    fi
else
    echo "✓ SaathiMart containers are running"
fi

# Check container health
echo ""
echo "Step 3: Verifying container health..."
CONTAINERS=("saathimart-backend" "saathimart-mariadb" "saathimart-redis-cache" "saathimart-redis-queue")

for container in "${CONTAINERS[@]}"; do
    if docker ps | grep -q "${container}"; then
        STATUS=$(docker inspect --format='{{.State.Health.Status}}' "${container}" 2>/dev/null || echo "unknown")
        echo "  ${container}: ${STATUS}"
    else
        echo "  ${container}: NOT RUNNING"
    fi
done

# Test API connectivity
echo ""
echo "Step 4: Testing API connectivity..."
API_RESPONSE=$(curl -s -o /dev/null -w "%{http_code}" -H "Host: saathimart.localhost" "http://localhost:8080/api/method/saathimart.api.products.list_products?page=1&page_size=1" 2>/dev/null || echo "000")

if [ "${API_RESPONSE}" = "200" ]; then
    echo "✓ API is accessible (HTTP ${API_RESPONSE})"
elif [ "${API_RESPONSE}" = "000" ]; then
    echo "❌ Cannot connect to API. Is the backend container running?"
    exit 1
else
    echo "⚠️  API returned HTTP ${API_RESPONSE}"
fi

# Run k6 load test
echo ""
echo "═══════════════════════════════════════════════════════════════════════"
echo "                        Running K6 Load Test                             "
echo "═══════════════════════════════════════════════════════════════════════"
echo ""

TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
REPORT_FILE="${REPORT_DIR}/k6-report-${TIMESTAMP}.json"
SUMMARY_FILE="${REPORT_DIR}/k6-summary-${TIMESTAMP}.txt"

echo "Test file: ${TEST_FILE}"
echo "Report file: ${REPORT_FILE}"
echo ""

# Run k6
k6 run \
    --out json="${REPORT_FILE}" \
    --summary-export="${REPORT_FILE}" \
    "${TEST_FILE}" 2>&1 | tee "${SUMMARY_FILE}"

echo ""
echo "═══════════════════════════════════════════════════════════════════════"
echo "                           Test Complete                                 "
echo "═══════════════════════════════════════════════════════════════════════"
echo ""
echo "Results saved to: ${REPORT_DIR}"
echo ""

# Parse results
if [ -f "${REPORT_FILE}" ]; then
    echo "Key Metrics:"
    echo ""
    cat "${SUMMARY_FILE}" | grep -E "(http_reqs|http_req_duration|http_req_failed|errors)" | head -10 || true
fi

echo ""
echo "For detailed analysis, run:"
echo "  cat ${SUMMARY_FILE}"
