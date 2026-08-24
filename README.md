### Saathi Middleware

Central middleware aggregating item/catalog data from franchise sites and serving the Next.js frontend

### Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch version-16
bench install-app saathi_middleware
```

### Local Development (Docker)

The fastest way to run the middleware locally is the bundled Docker stack — it builds its own bench, creates the `saathi-mw.localhost` site, and installs this app automatically.

```bash
docker compose up -d --build
```

This starts four containers (`db`, `redis-cache`, `redis-queue`, `mw`) and serves the site at **http://localhost:8002**, routed by the `Host: saathi-mw.localhost` header (Frappe is multi-tenant — requests must carry that header, e.g. `curl -H "Host: saathi-mw.localhost" http://localhost:8002/api/method/ping`). First boot takes a few minutes (bench init + site creation); watch progress with `docker logs -f saathi_middleware-mw-1`.

Set the Administrator password once the container is healthy:

```bash
docker exec -w /home/frappe/bench saathi_middleware-mw-1 bench --site saathi-mw.localhost set-admin-password <password>
```

**⚠️ `docker-compose.branch4.yml`**: this repo also ships a second compose file used to spin up an *isolated, parallel* stack (project `smw-branch4`, port 8013) for testing a feature branch without touching the primary dev stack. It only auto-applies if invoked explicitly:

```bash
docker compose -f docker-compose.yml -f docker-compose.branch4.yml up -d --build
```

Do **not** rename it back to `docker-compose.override.yml` — Compose auto-loads that filename on every plain `docker compose up`, which silently redirects you to the `smw-branch4` project/port instead of the primary stack on port 8002. This exact mixup previously caused the frontend (pointed at port 8002) to appear to be missing every feature that had actually been merged, because the merged code was only running in the separate `smw-branch4` container. If you need a second isolated stack again, base it on `docker-compose.branch4.yml`'s pattern but only load it with an explicit `-f`.

#### Registering a franchise

Each franchise site (see [`saathi_ecommerce`](../saathi_ecommerce)) authenticates to this middleware via a `Franchise` record's `api_key`/`api_secret`. Create one per franchise:

```python
# docker exec -w /home/frappe/bench/sites saathi_middleware-mw-1 \
#   /home/frappe/bench/env/bin/python3 - <<'EOF'
import frappe, secrets
frappe.init(site="saathi-mw.localhost"); frappe.connect()
doc = frappe.get_doc({
    "doctype": "Franchise",
    "site_code": "SF1",                 # must match Saathi Ecommerce Settings > Site Code on the franchise
    "franchise_name": "Saathi Franchise 1",
    "status": "Active",
    "api_key": secrets.token_hex(8),
    "api_secret": secrets.token_hex(8),
    "latitude": 27.7172, "longitude": 85.3240,
    "serviceable_radius_km": 15,
}).insert(ignore_permissions=True)
frappe.db.commit()
print(doc.site_code, doc.api_key, doc.get_password("api_secret"))
EOF
```

Hand the printed `site_code` / `api_key` / `api_secret` to the franchise site's `Saathi Ecommerce Settings` (see that app's README for the full item-sync setup and Docker-networking gotchas).

### Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/saathi_middleware
pre-commit install
```

Pre-commit is configured to use the following tools for checking and formatting your code:

- ruff
- eslint
- prettier
- pyupgrade

### License

mit
