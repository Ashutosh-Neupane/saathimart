#!/bin/bash
# saathi_middleware signup/signin test script
# Run these tests AFTER you have configured the email account in Frappe.
# Site: http://saathi-mw.localhost:8002

BASE="http://localhost:8002/api"
HEADER='-H "Host: saathi-mw.localhost" -H "Content-Type: application/json"'
TEST_EMAIL="testuser@example.com"

echo "=== 1. Signup ==="
curl -s -X POST "$BASE/method/saathi_middleware.api.auth_full.signup" \
  -H "Host: saathi-mw.localhost" \
  -H "Content-Type: application/json" \
  -d "{\"email\":\"$TEST_EMAIL\",\"full_name\":\"Test User\",\"contact\":\"9876543210\",\"password\":\"Test@1234\",\"phone\":\"9876543210\"}"

echo ""
echo "=== 2. Check OTP (check container logs) ==="
docker logs saathi_middleware-mw-1 --tail 20 | grep -i "OTP for"

echo ""
echo "=== 3. Verify OTP (replace OTP below with actual OTP from logs) ==="
curl -s -X POST "$BASE/method/saathi_middleware.api.auth_full.verify_signup_otp" \
  -H "Host: saathi-mw.localhost" \
  -H "Content-Type: application/json" \
  -d '{"email":"'"$TEST_EMAIL"'","otp":"REPLACE_WITH_ACTUAL_OTP"}'

echo ""
echo "=== 4. Login ==="
curl -s -X POST "$BASE/method/saathi_middleware.api.auth_full.login" \
  -H "Host: saathi-mw.localhost" \
  -H "Content-Type: application/json" \
  -d '{"usr":"'"$TEST_EMAIL"'","pwd":"Test@1234"}'
