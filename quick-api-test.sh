#!/bin/bash
# Quick API Test - Verifies all critical endpoints
# Run this before the load test to ensure APIs are working

BASE_URL="http://localhost:8080"
HOST="saathimart.localhost"

echo "═══════════════════════════════════════════════════════════════════════"
echo "              SaathiMart API Quick Test                                 "
echo "═══════════════════════════════════════════════════════════════════════"
echo ""

# Test function
test_api() {
    local name=$1
    local endpoint=$2
    local method=${3:-GET}
    
    printf "%-50s" "${name}..."
    
    if [ "$method" = "GET" ]; then
        RESPONSE=$(curl -s -w "\n%{http_code}" -H "Host: ${HOST}" "${BASE_URL}/api/method/${endpoint}" 2>/dev/null)
    else
        RESPONSE=$(curl -s -w "\n%{http_code}" -X POST -H "Host: ${HOST}" -H "Content-Type: application/json" "${BASE_URL}/api/method/${endpoint}" 2>/dev/null)
    fi
    
    HTTP_CODE=$(echo "$RESPONSE" | tail -n1)
    BODY=$(echo "$RESPONSE" | head -n -1)
    
    if [ "$HTTP_CODE" = "200" ]; then
        echo "✓ OK (200)"
        return 0
    elif [ "$HTTP_CODE" = "400" ]; then
        echo "⚠ BAD REQUEST (400) - May need parameters"
        return 1
    elif [ "$HTTP_CODE" = "401" ] || [ "$HTTP_CODE" = "403" ]; then
        echo "⚠ AUTH REQUIRED (${HTTP_CODE})"
        return 1
    elif [ "$HTTP_CODE" = "404" ]; then
        echo "✗ NOT FOUND (404)"
        return 1
    elif [ "$HTTP_CODE" = "500" ]; then
        echo "✗ SERVER ERROR (500)"
        echo "   Response: ${BODY:0:100}"
        return 1
    else
        echo "✗ HTTP ${HTTP_CODE}"
        return 1
    fi
}

# Test categories
echo "1. PRODUCT APIs"
echo "─────────────────────────────────────────────────────────────────────"
test_api "List Products" "saathimart.api.products.list_products?page=1&page_size=10"
test_api "Get Filters" "saathimart.api.filters.get_filters"
test_api "Search Products" "saathimart.api.search.search_products?query=test&page=1"
test_api "Top Searches" "saathimart.api.search.get_top_searches"
echo ""

echo "2. CART APIs"
echo "─────────────────────────────────────────────────────────────────────"
test_api "Get Cart" "saathimart.api.cart.get_cart?session_id=test123"
test_api "Get Cart Summary" "saathimart.api.cart.get_cart_summary?session_id=test123"
test_api "Get Cart Count" "saathimart.api.cart.get_cart_count?session_id=test123"
echo ""

echo "3. PAYMENT APIs"
echo "─────────────────────────────────────────────────────────────────────"
test_api "Get Payment Modes" "saathimart.api.payments.get_payment_modes"
echo ""

echo "4. DELIVERY APIs"
echo "─────────────────────────────────────────────────────────────────────"
test_api "Get Delivery Zones" "saathimart.api.delivery.get_delivery_zones"
echo ""

echo "5. CMS APIs"
echo "─────────────────────────────────────────────────────────────────────"
test_api "Get Site Config" "saathimart.api.cms.get_site_config"
test_api "Get Homepage Data" "saathimart.api.home.get_homepage_data"
test_api "Get Banners" "saathimart.api.home.get_banners"
test_api "Get Deals" "saathimart.api.home.get_deals"
test_api "Get Navigation" "saathimart.api.cms.get_navigation"
test_api "Get Quick Links" "saathimart.api.home.get_quick_links"
echo ""

echo "6. LOYALTY APIs"
echo "─────────────────────────────────────────────────────────────────────"
test_api "List Membership Plans" "saathimart.api.membership.list_plans"
test_api "Preview Redemption" "saathimart.api.loyalty.preview_redemption?points_to_redeem=100&order_subtotal=1000"
echo ""

echo "7. LOCATION APIs"
echo "─────────────────────────────────────────────────────────────────────"
test_api "Resolve Vendors" "saathimart.api.location.resolve_vendors?lat=27.7172&lng=85.3240&radius_km=5"
test_api "Get Popular Cities" "saathimart.api.cms.get_popular_cities"
echo ""

echo "8. SETTINGS APIs"
echo "─────────────────────────────────────────────────────────────────────"
test_api "Get Public Settings" "saathimart.api.settings.get_public_settings"
echo ""

echo "═══════════════════════════════════════════════════════════════════════"
echo "Test Complete"
echo "═══════════════════════════════════════════════════════════════════════"
