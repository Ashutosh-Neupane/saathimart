#!/bin/bash
set -euo pipefail

SITE="${FRAPPE_SITE:-saathimart.localhost}"
BENCH="/home/frappe/bench"
# Shared secret vendor sites sign their hub pushes with (X-SM-Secret header).
# Must match saathimart-vendor's WEBHOOK_SECRET — see docker/init.sh there.
WEBHOOK_SECRET="${WEBHOOK_SECRET:-saathimart-webhook-secret}"

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

# Seed Settings.webhook_secret so vendor pushes (X-SM-Secret) can be
# verified — without this every vendor->hub sync call is rejected.
#
# Must run with cwd = $BENCH/sites: Frappe's per-site log handler builds
# its file path as a *relative* join of site + "logs" + logfile (see
# frappe/utils/logger.py:create_handler), so frappe.connect() throws
# FileNotFoundError from anywhere else instead of writing to the real
# sites/<site>/logs directory.
echo "Configuring webhook secret..."
cd "$BENCH/sites"
"$BENCH/env/bin/python" - "$SITE" "$WEBHOOK_SECRET" <<'PYEOF'
import sys
import frappe
site = sys.argv[1]
secret = sys.argv[2]
frappe.init(site, sites_path='/home/frappe/bench/sites')
frappe.connect()
settings = frappe.get_single('Settings')
if not settings.get_password('webhook_secret', raise_exception=False):
    settings.webhook_secret = secret
    settings.save(ignore_permissions=True)
    frappe.db.commit()
    print('  Webhook secret configured')
else:
    print('  Webhook secret already configured, leaving as-is')
PYEOF
cd "$BENCH"

# Seed System Settings.currency so every Currency-fieldtype field (Order
# totals, Vendor Payout amounts, Product prices, ...) formats with the Rs
# symbol instead of Frappe's factory-default USD/$ — this is the framework's
# own global currency default, separate from (and not set by) this app's own
# Settings.currency field, which only covers this app's own "NPR 123"-style
# text formatting, not native Currency field rendering.
echo "Configuring default currency..."
cd "$BENCH/sites"
"$BENCH/env/bin/python" - "$SITE" <<'PYEOF'
import sys
import frappe
site = sys.argv[1]
frappe.init(site, sites_path='/home/frappe/bench/sites')
frappe.connect()
if frappe.db.exists('Currency', 'NPR') and not frappe.db.get_value('Currency', 'NPR', 'enabled'):
    frappe.db.set_value('Currency', 'NPR', 'enabled', 1)
# frappe.db.set_value (not get_single().save()) — System Settings has other
# mandatory fields (language, time_zone) that a fresh site never had a Setup
# Wizard run to fill in, so a normal .save() throws MandatoryError even
# though currency is the only field actually being touched here.
if frappe.db.get_value('System Settings', 'System Settings', 'currency') != 'NPR':
    frappe.db.set_value('System Settings', 'System Settings', 'currency', 'NPR')
    frappe.db.commit()
    print('  Default currency set to NPR')
else:
    print('  Default currency already NPR, leaving as-is')
PYEOF
cd "$BENCH"

# `frappe.app:application` (the raw WSGI callable gunicorn would otherwise
# import) never serves /assets or /files — that middleware only gets
# applied inside frappe.app.serve(), which `bench serve`/`bench start`'s
# dev server calls but a bare gunicorn import never does. In a real
# deployment nginx serves those paths directly from disk instead; this
# compose stack has no nginx in front, so wrap the app here or every JS/CSS
# asset 404s once real gunicorn workers (not `bench start`'s werkzeug dev
# server) are serving requests.
cat > "$BENCH/sites/gunicorn_wsgi.py" <<'EOF'
import frappe.app
application = frappe.app.application_with_statics()
EOF

# Create Procfile for bench start with full gunicorn path.
#
# web-only was silently dropping the entire async side of this app: every
# frappe.enqueue() call (order.new delivery, the instant-delivery path
# added for order/product/barcode sync, etc.) just piles up in Redis
# forever with nothing consuming it, and every scheduler cron job
# (drain_event_queue, archive_old_data, reconcile jobs, payment polling)
# never fires at all without a `bench schedule` process ticking. Confirmed
# live via `bench doctor` — Workers online: 0, hundreds of jobs queued and
# never processed, after days of this container running web-only.
#
# worker/schedule (like the webhook-secret-seeding step earlier in this
# script) must run with cwd = $BENCH/sites, not bench root: Frappe's
# per-site log handler builds its file path as a relative join of site +
# "logs" + logfile, so any other cwd throws FileNotFoundError trying to
# open sites/<site>/logs/database.log — confirmed live, worker/schedule
# crash-looped every few seconds against $BENCH before this fix.
cat > "$BENCH/Procfile" <<'EOF'
web: cd /home/frappe/bench/sites && /home/frappe/bench/env/bin/gunicorn --bind 0.0.0.0:8000 --workers ${WORKERS:-2} --threads ${THREADS:-4} gunicorn_wsgi:application
worker: cd /home/frappe/bench/sites && /usr/local/bin/bench worker --queue short,default,long
schedule: cd /home/frappe/bench/sites && /usr/local/bin/bench schedule
EOF

echo "=== SaathiMart ready at http://localhost:8000 ==="
exec bench start
