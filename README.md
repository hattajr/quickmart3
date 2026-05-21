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

1. Fill `.env.prod` with the production S3 values, `SESSION_SECRET_KEY`, and `ADMIN_PASSWORD`.
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
docker compose up -d --build
```

`docker-compose.yml` loads `.env.prod` automatically for production services.

App URLs:

- App: `http://<host>:8756`
- Grafana: `http://<host>:3000` (configured credentials)
- sqlite-web: `http://<host>:7756`

## Dev

Run the app with the dev launcher:

```bash
uv run --env-file .env.dev run_dev.py
```

Useful `.env.dev` flags:

- `DEV_DB_RESET_ON_START=1` deletes the dev DB before startup
- `DEV_DB_CLEANUP_ON_EXIT=1` deletes the dev DB on shutdown

Set either flag to `0` if you want to keep the local dev database between runs.
