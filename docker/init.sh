#!/bin/bash
set -euo pipefail

SITE="${FRAPPE_SITE:-saathi-mw.localhost}"
BENCH="/home/frappe/bench"

wait_for() {
  local host=$1 port=$2 label=$3
  echo "Waiting for $label..."
  for i in $(seq 1 60); do
    if nc -z "$host" "$port" 2>/dev/null; then echo "$label ready."; return; fi
    [ "$i" -eq 60 ] && echo "ERROR: $label not ready" && exit 1
    sleep 2
  done
}

wait_for "${DB_HOST:-db}"         "${DB_PORT:-3306}"  "MariaDB"
wait_for "${REDIS_HOST:-redis-cache}"  "${REDIS_PORT:-6379}" "Redis"

if [ ! -f "$BENCH/.frappe_installed" ]; then
  rm -rf "$BENCH/apps/frappe"
  echo "Initialising bench..."
  bench init "$BENCH" \
    --frappe-branch version-16 \
    --no-procfile \
    --skip-redis-config-generation \
    --ignore-exist
  touch "$BENCH/.frappe_installed"
fi

cd "$BENCH"

mkdir -p "$BENCH/sites" "$BENCH/sites/assets" "$BENCH/logs" "/home/frappe/logs"
chown -R frappe:frappe "$BENCH/sites" "$BENCH/logs" "/home/frappe/logs"

bench set-config -g redis_cache    "redis://${REDIS_HOST:-redis-cache}:${REDIS_PORT:-6379}/${REDIS_DB:-0}"
bench set-config -g redis_queue    "redis://${REDIS_QUEUE_HOST:-redis-queue}:${REDIS_QUEUE_PORT:-6379}/${REDIS_DB:-0}"
bench set-config -g redis_socketio "redis://${REDIS_HOST:-redis-cache}:${REDIS_PORT:-6379}/${REDIS_DB:-0}"
bench set-config -g gunicorn_bind "0.0.0.0:8000"
bench set-config -g db_host "${DB_HOST:-db}"

if [ ! -d "$BENCH/apps/saathi_middleware" ]; then
  ln -s /home/frappe/saathi_middleware "$BENCH/apps/saathi_middleware"
  echo "saathi_middleware" >> "$BENCH/apps/apps.txt"
fi
if ! "$BENCH/env/bin/python" -c "import saathi_middleware" >/dev/null 2>&1; then
  uv pip install --quiet --no-deps -e "$BENCH/apps/saathi_middleware" --python "$BENCH/env/bin/python"
fi
if ! grep -qx "saathi_middleware" "$BENCH/sites/apps.txt" 2>/dev/null; then
  [ -s "$BENCH/sites/apps.txt" ] && [ -n "$(tail -c1 "$BENCH/sites/apps.txt")" ] && echo >> "$BENCH/sites/apps.txt"
  echo "saathi_middleware" >> "$BENCH/sites/apps.txt"
fi

if [ ! -d "$BENCH/sites/$SITE" ]; then
  echo "Creating site $SITE..."
  cd "$BENCH"
  bench new-site "$SITE" \
    --mariadb-root-username root \
    --mariadb-root-password "${DB_ROOT_PASSWORD:-admin}" \
    --admin-password "${ADMIN_PASSWORD:-admin}" \
    --db-host "${DB_HOST:-db}" \
    --db-port "${DB_PORT:-3306}" \
    --no-mariadb-socket
  touch "$BENCH/sites/$SITE/.installed"
fi

if [ ! -f "$BENCH/sites/$SITE/.app_installed" ]; then
  cd "$BENCH"
  bench --site "$SITE" install-app saathi_middleware
  touch "$BENCH/sites/$SITE/.app_installed"
fi

echo "Running migrations..."
cd "$BENCH"
bench --site "$SITE" migrate
bench build --app saathi_middleware || true

echo "Configuring email..."
cd "$BENCH/sites"
"$BENCH/env/bin/python" - "$SITE" <<'PYEOF'
import sys
import os
import json
import frappe
site = sys.argv[1]
frappe.init(site, sites_path='/home/frappe/bench/sites')
frappe.connect()

email_host = os.environ.get('EMAIL_HOST', 'smtp.gmail.com')
email_port = os.environ.get('EMAIL_PORT', '587')
email_use_tls = os.environ.get('EMAIL_USE_TLS', '1')
email_host_user = os.environ.get('EMAIL_HOST_USER', '')
email_host_password = os.environ.get('EMAIL_HOST_PASSWORD', '')
email_default_sender = os.environ.get('EMAIL_DEFAULT_SENDER', email_host_user)

config_path = f'/home/frappe/bench/sites/{site}/site_config.json'
with open(config_path, 'r') as f:
    config = json.load(f)

config['mail_server'] = email_host
config['smtp_port'] = int(email_port)
config['use_tls'] = int(email_use_tls)
config['mail_login'] = email_host_user
config['mail_password'] = email_host_password
config['auto_email_id'] = email_default_sender

with open(config_path, 'w') as f:
    json.dump(config, f, indent=1)

frappe.db.commit()
print('  Email configured in site_config.json')
PYEOF

cd "$BENCH"

cat > "$BENCH/sites/gunicorn_wsgi.py" <<'EOF'
import frappe.app
application = frappe.app.application_with_statics()
EOF

cat > "$BENCH/Procfile" <<'EOF'
web: cd /home/frappe/bench/sites && /home/frappe/bench/env/bin/gunicorn --bind 0.0.0.0:8000 gunicorn_wsgi:application
worker: cd /home/frappe/bench/sites && /usr/local/bin/bench worker --queue short,default,long
schedule: cd /home/frappe/bench/sites && /usr/local/bin/bench schedule
EOF

echo "=== Saathi Middleware ready at http://localhost:8002 ==="
exec bench start
