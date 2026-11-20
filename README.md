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

## Supplier invoices

Each week, photograph the supplier's invoice (거래명세표 or web order sheet) and load it on the host:

```bash
uv run python -m app.services.supplier_invoices --db data/quickmart.sqlite3 photo1.jpg [photo2.jpg ...]
```

It runs headless Claude Code (`claude -p`, logged in on the host) to turn each photo upright and read it, then stores one invoice per supplier and date in the `supplier_*` tables, which are separate from the shop's `products`:

- Pages of one invoice can be separate photos; they are joined by the printed page marker (e.g. `16-1/2`).
- An invoice is stored only if every row's qty × price equals its amount and the rows add up to the printed totals.
- A date already stored is skipped. If two different copies of one date are given, the newest photo wins.
- Each page keeps its upright photo (`supplier_invoice_pages.image_jpeg`) and the model's raw answer for double-checking.
- `supplier_products.name_latin` is the product's identity. Rename it there to fix a name on every invoice.
- `supplier_price_history` shows each product's unit price over time, without crossed-out or free rows.

Answers are cached in `data/supplier_invoice_cache/`, so a rerun costs nothing.

### From the phone: the admin Invoices tab

The admin page (`/admin`) has two tabs, built for a phone: **Products** to search, add, edit and delete products, and **Invoices** to send this week's invoice photos (Take photo or From gallery, one photo per page) and check prices.

The app only queues the photos (`supplier_invoice_uploads`). The `invoice-worker` container reads them with the same `claude -p` reader as above, one upload at a time. It is the app image plus Claude Code (version pinned in the `Dockerfile`), and it signs in with a subscription token:

1. Run `claude setup-token` on any machine where you can open a browser, and put the token in `.env.prod` as `CLAUDE_CODE_OAUTH_TOKEN`.
2. `docker compose --env-file .env.prod up -d --build` starts it with the rest of the stack. After changing the token, `docker compose --env-file .env.prod up -d invoice-worker` restarts it with the new one.

For dev, `make invoice-worker` runs the same worker on your machine with its own `claude` login (`DB=` picks the database).

Everything that goes wrong shows on the Invoices tab, so the phone always says whether reading works:

- **The reader isn't running:** it writes a heartbeat file beside the database (`.supplier-invoice-worker.json`) every 10 seconds; after 45 seconds without one, the tab says so with the commands to start it and see its logs.
- **The token is missing or expired, the usage limit is reached, Claude is overloaded, or `claude` is missing:** these stop every photo, so the upload goes back in line with the reason on its card, the tab shows Claude's own message and what to do, and the worker tries again by itself (every 5 minutes for the login, 15 for the limit).
- **One photo fails** (unreadable, Claude timed out after 10 minutes, rows that don't add up): the upload fails with the reason per photo and a Try again button.
- **A price update or match change fails:** a red toast with the reason; server errors show their status code.

When an invoice is read, the phone opens its price check:

- A product is listed to fix when the shop price is under cost +10%. The new price starts at cost +15%, rounded up to the next 100; +5% to +50% buttons or typing change it.
- Cost per piece: a unit price equal to the amount with a per-box count is a box price and is divided by the count; a unit price below the amount is already per piece; an order-sheet note like `Quantity : 3 pcs` divides by 3.
- **Update price** sets the shop `price` and `purchase_price` (the cost) and remembers the shop product for that supplier product (`supplier_product_matches`). **Skip** leaves it. **Undo** puts the old price back unless it was changed again since. Each decision is in `supplier_price_reviews`.
- Shop products are guessed by name (rapidfuzz) until one is confirmed; **Change** picks another or marks it as not sold in the shop, which leaves it out from then on.
