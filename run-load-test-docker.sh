#!/bin/bash
# SaathiMart K6 Load Test via Docker
# Runs k6 inside Docker container to test APIs

set -e

echo "═══════════════════════════════════════════════════════════════════════"
echo "               SaathiMart K6 Load Test - Docker Version                 "
echo "═══════════════════════════════════════════════════════════════════════"

# Wait for Docker daemon
echo "Waiting for Docker daemon..."
for i in {1..30}; do
    if docker ps &>/dev/null; then
        echo "✓ Docker is ready"
        break
    fi
    sleep 1
done

# Check containers
cd /Users/ashutoshneupane/Desktop/School_saas/saathimart

if ! docker ps | grep -q "saathimart-backend"; then
    echo "Starting SaathiMart containers..."
    docker-compose up -d
    sleep 60
fi

# Run k6 from Docker container
echo "Running k6 load test..."
docker run --rm \
    --network host \
    -v "$(pwd)/k6-critical-apis.js:/test.js" \
    -v "$(pwd)/load-test-results:/results" \
    grafana/k6:latest run \
    --out json=/results/k6-report.json \
    /test.js

echo "Test complete! Results in ./load-test-results/"
