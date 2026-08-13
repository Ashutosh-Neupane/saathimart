# saathi_middleware — Manual Test Checklist

## Prerequisites
- Container is running and healthy: `docker ps --filter name=saathi_middleware-mw-1`
- Site responds: `curl -s -o /dev/null -w "%{http_code}" http://localhost:8002/api/method/ping -H "Host: saathi-mw.localhost"` → `200`
- Email account configured in Frappe (you said you will set this up)

## Test 1: Signup
```bash
curl -s -X POST http://localhost:8002/api/method/saathi_middleware.api.auth_full.signup \
  -H "Host: saathi-mw.localhost" \
  -H "Content-Type: application/json" \
  -d '{"email":"testuser@example.com","full_name":"Test User","contact":"9876543210","password":"Test@1234","phone":"9876543210"}'
```
Expected: `{"message":"Verification code sent","email":"testuser@example.com"}`

## Test 2: Get OTP from logs
```bash
docker logs saathi_middleware-mw-1 --tail 30 | grep -i "OTP for"
```
Expected: `[dev] OTP for testuser@example.com (signup): 123456`

## Test 3: Verify OTP
Replace `123456` with actual OTP from logs.
```bash
curl -s -X POST http://localhost:8002/api/method/saathi_middleware.api.auth_full.verify_signup_otp \
  -H "Host: saathi-mw.localhost" \
  -H "Content-Type: application/json" \
  -d '{"email":"testuser@example.com","otp":"123456"}'
```
Expected: `{"message":"Account verified successfully","email":"testuser@example.com","api_key":"...","api_secret":"...","token":"..."}`

## Test 4: Login
```bash
curl -s -X POST http://localhost:8002/api/method/saathi_middleware.api.auth_full.login \
  -H "Host: saathi-mw.localhost" \
  -H "Content-Type: application/json" \
  -d '{"usr":"testuser@example.com","pwd":"Test@1234"}'
```
Expected: `{"message":"Logged in","user":"testuser@example.com",...}`

## Test 5: Check User in Frappe
1. Open http://localhost:8002 (Host: saathi-mw.localhost)
2. Login as Administrator
3. Go to User list
4. Search for `testuser@example.com`
5. Verify: Enabled = 1, roles include SM Customer
