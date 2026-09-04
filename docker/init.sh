#!/bin/bash
set -euo pipefail

SITE="${FRAPPE_SITE:-saathimart.localhost}"
BENCH="/home/frappe/bench"
WEBHOOK_SECRET="${WEBHOOK_SECRET:-saathimart-webhook-secret}"

# ── Wait for dependencies ─────────────────────────────────────────────────────
wait_for() {
  local host=$1 port=$2 label=$3
  echo "Waiting for $label at $host:$port ..."
  for i in $(seq 1 60); do
    if nc -z "$host" "$port" 2>/dev/null; then
      echo "  $label ready."
      return
    fi
    [ "$i" -eq 60 ] && echo "ERROR: $label not ready after 120s" && exit 1
    sleep 2
  done
}

wait_for "${DB_HOST:-mariadb}"        "${DB_PORT:-3306}"        "MariaDB"
wait_for "${REDIS_HOST:-redis-cache}" "${REDIS_PORT:-6379}"     "Redis cache"
wait_for "${REDIS_QUEUE_HOST:-redis-queue}" "${REDIS_QUEUE_PORT:-6379}" "Redis queue"

# ── Init bench (Frappe only — no ERPNext) ─────────────────────────────────────
if [ ! -f "$BENCH/.frappe_installed" ]; then
  rm -rf "$BENCH/apps/frappe"
  echo "Initialising bench (frappe version-16)..."
  bench init "$BENCH" \
    --frappe-branch version-16 \
    --no-procfile \
    --skip-redis-config-generation \
    --ignore-exist
  touch "$BENCH/.frappe_installed"
fi

cd "$BENCH"

# Ensure writable dirs
mkdir -p "$BENCH/sites" "$BENCH/sites/assets" "$BENCH/logs" "/home/frappe/logs"
chown -R frappe:frappe "$BENCH/sites" "$BENCH/logs" "/home/frappe/logs" 2>/dev/null || true

# ── Redis config ──────────────────────────────────────────────────────────────
bench set-config -g redis_cache    "redis://${REDIS_HOST:-redis-cache}:${REDIS_PORT:-6379}/${REDIS_DB:-0}"
bench set-config -g redis_queue    "redis://${REDIS_QUEUE_HOST:-redis-queue}:${REDIS_QUEUE_PORT:-6379}/${REDIS_DB:-0}"
bench set-config -g redis_socketio "redis://${REDIS_HOST:-redis-cache}:${REDIS_PORT:-6379}/${REDIS_DB:-0}"
bench set-config -g gunicorn_bind  "0.0.0.0:8000"
bench set-config -g db_host        "${DB_HOST:-mariadb}"

# ── Link saathimart app ───────────────────────────────────────────────────────
# Volume mount: ..:/home/frappe/saathimart-src (repo root, read-only)
# App source lives at: /home/frappe/saathimart-src/saathimart/
APP_SRC="/home/frappe/saathimart-src/saathimart"
if [ ! -d "$BENCH/apps/saathimart" ]; then
  ln -s "$APP_SRC" "$BENCH/apps/saathimart"
  echo "saathimart" >> "$BENCH/apps/apps.txt"
fi

# Register on Python path (editable install)
if ! "$BENCH/env/bin/python" -c "import saathimart" >/dev/null 2>&1; then
  "$BENCH/env/bin/pip" install --quiet --no-deps -e "$APP_SRC"
fi

# Register in sites/apps.txt (frappe.get_all_apps reads this)
if ! grep -qx "saathimart" "$BENCH/sites/apps.txt" 2>/dev/null; then
  [ -s "$BENCH/sites/apps.txt" ] && \
    [ -n "$(tail -c1 "$BENCH/sites/apps.txt")" ] && \
    echo >> "$BENCH/sites/apps.txt"
  echo "saathimart" >> "$BENCH/sites/apps.txt"
fi

# ── Create site ───────────────────────────────────────────────────────────────
if [ ! -d "$BENCH/sites/$SITE" ]; then
  echo "Creating site $SITE ..."
  bench new-site "$SITE" \
    --mariadb-root-password "${DB_ROOT_PASSWORD:-root}" \
    --admin-password        "${ADMIN_PASSWORD:-admin}" \
    --db-host               "${DB_HOST:-mariadb}" \
    --db-port               "${DB_PORT:-3306}" \
    --no-mariadb-socket
fi

# ── Install app ───────────────────────────────────────────────────────────────
if [ ! -f "$BENCH/sites/$SITE/.app_installed" ]; then
  echo "Installing saathimart on $SITE ..."
  bench --site "$SITE" install-app saathimart
  touch "$BENCH/sites/$SITE/.app_installed"
fi

# ── Migrate (safe to re-run) ──────────────────────────────────────────────────
echo "Running migrations ..."
bench --site "$SITE" migrate

# ── Build assets ──────────────────────────────────────────────────────────────
bench build --app saathimart || true

# ── Post-install config (run from sites/ so Frappe log paths resolve) ─────────
echo "Applying post-install config ..."
cd "$BENCH/sites"

"$BENCH/env/bin/python" - "$SITE" "$WEBHOOK_SECRET" <<'PYEOF'
import sys, frappe

site, secret = sys.argv[1], sys.argv[2]
frappe.init(site, sites_path='/home/frappe/bench/sites')
frappe.connect()

# 1. Webhook secret
s = frappe.get_single('Settings')
if not s.get_password('webhook_secret', raise_exception=False):
    s.webhook_secret = secret
    s.save(ignore_permissions=True)
    frappe.db.commit()
    print('  Webhook secret configured')

# 2. Default currency → NPR
if frappe.db.exists('Currency', 'NPR'):
    frappe.db.set_value('Currency', 'NPR', 'enabled', 1)
if frappe.db.get_value('System Settings', 'System Settings', 'currency') != 'NPR':
    frappe.db.set_value('System Settings', 'System Settings', 'currency', 'NPR')
    frappe.db.commit()
    print('  Default currency set to NPR')

frappe.destroy()
PYEOF

cd "$BENCH"

# ── WSGI wrapper (serves /assets and /files without nginx) ───────────────────
cat > "$BENCH/sites/gunicorn_wsgi.py" <<'EOF'
import frappe.app
application = frappe.app.application_with_statics()
EOF

# ── Procfile (web + worker + scheduler) ──────────────────────────────────────
# Optimized for 1000 user target:
# - 6 workers (2 per core) + gthread for async I/O
# - 4 threads per worker for parallel request handling
# - Reduced memory footprint with proper timeout settings
cat > "$BENCH/Procfile" <<'EOF'
web: cd /home/frappe/bench/sites && /home/frappe/bench/env/bin/gunicorn --bind 0.0.0.0:8000 --workers ${WORKERS:-6} --threads ${THREADS:-4} --timeout ${WORKER_TIMEOUT:-60} --keep-alive 5 --max-requests 1000 --max-requests-jitter 200 --graceful-timeout 15 --worker-class gthread gunicorn_wsgi:application
worker: cd /home/frappe/bench/sites && /usr/local/bin/bench worker --queue short,default,long
schedule: cd /home/frappe/bench/sites && /usr/local/bin/bench schedule
EOF

echo "=== SaathiMart ready at http://localhost:8000 ==="
exec bench start
