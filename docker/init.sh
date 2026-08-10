#!/bin/bash
set -euo pipefail

SITE="${FRAPPE_SITE:-saathimart.localhost}"
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

wait_for "${DB_HOST:-mariadb}"         "${DB_PORT:-3306}"  "MariaDB"
wait_for "${REDIS_HOST:-redis-cache}"  "${REDIS_PORT:-6379}" "Redis"

# Init bench only if a previous attempt fully completed (partial/failed
# attempts leave apps/frappe on disk but must not be mistaken for done,
# or bench's interactive "overwrite?" prompt aborts with no stdin attached)
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

# Ensure all mounted directories are writable by the frappe user.
# Docker named volumes are created as root; bench runs as frappe.
mkdir -p "$BENCH/sites" "$BENCH/sites/assets" "$BENCH/logs" "/home/frappe/logs"
chown -R frappe:frappe "$BENCH/sites" "$BENCH/logs" "/home/frappe/logs"

# Configure Redis. REDIS_DB isolates this bench's keyspace from any other
# bench sharing the same Redis server — Frappe caches some data (e.g.
# assets_json) without a site-name prefix, so two benches on DB 0 silently
# clobber each other's asset manifests.
bench set-config -g redis_cache    "redis://${REDIS_HOST:-redis-cache}:${REDIS_PORT:-6379}/${REDIS_DB:-0}"
bench set-config -g redis_queue    "redis://${REDIS_QUEUE_HOST:-redis-queue}:${REDIS_QUEUE_PORT:-6379}/${REDIS_DB:-0}"
bench set-config -g redis_socketio "redis://${REDIS_HOST:-redis-cache}:${REDIS_PORT:-6379}/${REDIS_DB:-0}"
bench set-config -g gunicorn_bind "0.0.0.0:8000"
bench set-config -g db_host "${DB_HOST:-mariadb}"

# Link saathimart app and register it as an editable install, same as
# bench does for apps it clones itself (symlinking alone never puts the
# app on the venv's Python path)
if [ ! -d "$BENCH/apps/saathimart" ]; then
  ln -s /home/frappe/saathimart "$BENCH/apps/saathimart"
  echo "saathimart" >> "$BENCH/apps/apps.txt"
fi
if ! "$BENCH/env/bin/python" -c "import saathimart" >/dev/null 2>&1; then
  uv pip install --quiet --no-deps -e "$BENCH/apps/saathimart" --python "$BENCH/env/bin/python"
fi
# frappe.get_all_apps() (used by install-app) reads sites/apps.txt, not
# apps/apps.txt — bench get-app maintains this automatically, but a
# manually symlinked local app needs it added explicitly
if ! grep -qx "saathimart" "$BENCH/sites/apps.txt" 2>/dev/null; then
  # apps.txt may not end in a newline — appending blindly would merge
  # onto the last line (e.g. "frappesaathimart")
  [ -s "$BENCH/sites/apps.txt" ] && [ -n "$(tail -c1 "$BENCH/sites/apps.txt")" ] && echo >> "$BENCH/sites/apps.txt"
  echo "saathimart" >> "$BENCH/sites/apps.txt"
fi

# Create site
if [ ! -d "$BENCH/sites/$SITE" ]; then
  echo "Creating site $SITE..."
  cd "$BENCH"
  bench new-site "$SITE" \
    --mariadb-root-username "${DB_ROOT_PASSWORD:-root}" \
    --mariadb-root-password "${DB_ROOT_PASSWORD:-root}" \
    --admin-password "${ADMIN_PASSWORD:-admin}" \
    --db-host "${DB_HOST:-mariadb}" \
    --db-port "${DB_PORT:-3306}" \
    --no-mariadb-socket
  touch "$BENCH/sites/$SITE/.installed"
fi

if [ ! -f "$BENCH/sites/$SITE/.app_installed" ]; then
  cd "$BENCH"
  bench --site "$SITE" install-app saathimart
  touch "$BENCH/sites/$SITE/.app_installed"
fi

# Migrate on every start, not just the first — app code (new doctypes,
# fields, fixtures) changes far more often than the site itself, and
# `bench migrate` is safe/idempotent to re-run with nothing new to do.
echo "Running migrations..."
cd "$BENCH"
bench --site "$SITE" migrate
bench build --app saathimart || true

# Create Procfile for bench start with full gunicorn path
cat > "$BENCH/Procfile" <<'EOF'
web: cd /home/frappe/bench/sites && /home/frappe/bench/env/bin/gunicorn --bind 0.0.0.0:8000 frappe.app:application
EOF

echo "=== SaathiMart ready at http://localhost:8000 ==="
exec bench start
