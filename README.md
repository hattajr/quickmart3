# quickmart3

## What

QuickMart is a FastAPI app backed by local SQLite. Product and carousel images stay in Supabase Storage. SQLite backups replicate to Supabase S3-compatible storage through Litestream.

## Stack

- Python 3.13
- FastAPI + Jinja2 + Uvicorn
- SQLite
- Litestream
- sqlite-web
- Tailwind CSS
- Docker Compose
- uv

## Deploy

1. Copy `.env.example` to `.env.prod` and fill it with production-only values. Use long, unique values for `SESSION_SECRET_KEY`, `ADMIN_PASSWORD`, and `GRAFANA_ADMIN_PASSWORD`.
2. Create the `sqlite-backup` bucket in Supabase Storage.
3. Import the live data into `data/quickmart.sqlite3`:

```bash
PG_HOST=... \
PG_PORT=5432 \
PG_DATABASE=postgres \
PG_USER=... \
PG_PASSWORD=... \
SQLITE_PATH=data/quickmart.sqlite3 \
uv run migrations/import_from_supabase.py
```

4. Start the stack:

```bash
docker compose --env-file .env.prod up -d --build
```

The compose file keeps application and monitoring ports on localhost. Put an HTTPS reverse proxy or Tailscale Serve in front of the app instead of publishing these ports directly. Set `SESSION_COOKIE_SECURE=1` when HTTPS is enabled.

App URLs from the host:

- App: `http://127.0.0.1:8756`
- Grafana: `http://127.0.0.1:3000`

SQLite Web is an optional local-only tool. Start it only when needed with:

```bash
docker compose --env-file .env.prod --profile tools up -d sqlite-web
```

## Dev

Run the app with the dev launcher:

```bash
uv run --env-file .env.dev run_dev.py
```

Useful `.env.dev` flags (copy `.env.example` first):

- `DEV_DB_RESET_ON_START=1` deletes the dev DB before startup
- `DEV_DB_CLEANUP_ON_EXIT=1` deletes the dev DB on shutdown

Set either flag to `0` if you want to keep the local dev database between runs.
