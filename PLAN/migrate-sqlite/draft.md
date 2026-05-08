# SQLite Migration Draft

## Goal

~~Move app data reads and writes from remote Supabase Postgres to local SQLite to remove search latency. Keep image storage in Supabase and add SQLite backup replication with Litestream to Supabase object storage.~~ Move app data reads and writes from remote Supabase Postgres to local SQLite to remove search latency, using the current Supabase schema as the source of truth for migrated tables. Keep image storage in Supabase and add SQLite backup replication with Litestream to Supabase object storage.

## Scope

- ~~Migrate the app tables used directly by the code: `products`, `sold_items`, `sold_sessions`, and search logging.~~ Migrate the app-active tables from the current Supabase schema: `products`, `categories`, `sold_items`, `sold_sessions`, `search_selections`, and `feedback_messages`.
- Replace the current `psycopg2` database layer with a SQLite-backed layer.
- Update route SQL for SQLite differences such as placeholders, `ILIKE`, `ANY(...)`, and `NOW()`.
- ~~Add a migration/import path to copy current product data from Supabase into SQLite.~~ Add a migration/import path to copy current data for all in-scope tables from Supabase into SQLite.
- Add Litestream replication for the SQLite database to Supabase object storage.
- Add Docker Compose for running the app with persistent SQLite storage and Litestream config.
- Add `sqlite-web` as a web UI for inspecting and editing SQLite data.
- Keep Supabase Storage for product and carousel images.
- Preserve the important constraints and indexes from Supabase where they affect app behavior, including `products.name` uniqueness, `sold_sessions.session_id` uniqueness, the `sold_items.item_id -> products.id` foreign key, and lookup indexes used by admin and reporting flows.
- Translate the relevant Postgres types and defaults into SQLite equivalents while preserving app behavior, including `serial4` and `bigserial`, `timestamptz`, `inet`, `bpchar(1)`, and timestamp defaults.

## Schema Basis

- Supabase is the schema source of truth for this migration draft.
- `products` includes fields the original draft did not call out explicitly: `category_id`, `created_at`, `updated_at`, `purchase_price`, and `latest_price`, plus a unique constraint on `name`.
- `categories` is included because `products.category_id` exists in the real schema and should not be dropped into an orphaned integer field during the move.
- `feedback_messages` is part of the operational schema and includes `created_at`, `is_deleted`, and multiple lookup indexes.
- `search_selections` and `search_logs` are separate tables in Supabase. The app-active migration scope includes `search_selections`; `search_logs` is excluded unless a later requirement proves it is needed.

## Non-Goals

- Do not migrate Supabase Storage.
- Do not support multi-instance or multi-writer deployment in this change.
- ~~Do not preserve Supabase auth, storage, or other Postgres-only schemas.~~ Do not preserve Supabase auth, storage, or unrelated Postgres-only schemas and tables such as `events`, `event_code`, `orders`, `order_items`, `sessions`, `schema_migrations`, or `search_logs`.
- Do not support active-active SQLite replication.
- Do not build a custom admin database editor in the app.

## Why Now

Current benchmark shows the bottleneck is remote DB latency, not RapidFuzz.

- Postgres search pipeline: about `1.37s`
- SQLite search pipeline: about `2.7ms`

Most of the delay comes from the two DB round-trips in the search path.

## Risks

- SQLite is a good fit only if deployment stays on one machine with persistent disk.
- We need a clean backup and restore path before cutover.
- Litestream must be configured against Supabase's S3-compatible endpoint, not AWS defaults.
- ~~Docker Compose becomes part of the deployment contract if we package app, volume, and Litestream together.~~ Docker Compose becomes part of the deployment contract for packaging the app, persistent volume, Litestream, and `sqlite-web` together.
- `sqlite-web` must be protected from public exposure and should stay internal-only.
- ~~We need to resolve the current `search_logs` vs `search_selections` schema mismatch.~~ Supabase contains both `search_logs` and `search_selections`; this migration intentionally includes only `search_selections` unless a later requirement proves `search_logs` is still needed.
- SQLite schema translation must preserve the important defaults, checks, and relationships from Supabase closely enough that app behavior does not drift, especially around `feedback_messages.is_deleted`, product uniqueness, and foreign key integrity.
- Admin writes, checkout writes, and startup behavior must still work after the swap.

## Deployment Shape

- ~~App uses a local SQLite file on a mounted volume.~~ ~~App uses a local SQLite database directory under `./app/db/database/` on a mounted Docker volume so the primary database file and SQLite sidecar files such as WAL and SHM persist across container restarts while remaining local to the deployment host.~~ App uses a local SQLite database directory under `./data/` at the repo root, bind-mounted to `/data/` in every Compose service so the primary database file and SQLite sidecar files such as WAL and SHM persist across container restarts while remaining local to the deployment host.
- ~~Litestream continuously replicates that SQLite file to Supabase object storage.~~ Litestream continuously replicates that SQLite file to Supabase object storage for backup and restore only.
- Restore flow must be defined before app start when the local DB is missing.
- ~~Docker Compose likely becomes the simplest way to package app container, mounted data volume, and Litestream configuration.~~ Docker Compose packages the app container, mounted data volume, Litestream configuration, and `sqlite-web` service.
- ~~`sqlite-web` runs alongside the app for direct table inspection and controlled data edits.~~ `sqlite-web` runs alongside the app for direct table inspection and controlled data edits, and stays internal-only.
- SQLite connections initialize required runtime settings on open, including WAL mode, foreign keys enabled, a busy timeout, and a balanced synchronous mode.
- ~~The SQLite database file path is treated as generated runtime data and must stay ignored by git.~~ ~~The SQLite database directory is treated as generated runtime data and must stay ignored by git so the main database file, WAL file, SHM file, and related runtime artifacts are never committed.~~ The `./data/` directory is treated as generated runtime data and must stay ignored by git so the main database file, WAL file, SHM file, and related runtime artifacts are never committed.

## Desired Outcome

~~The app uses local SQLite as its primary operational database, search becomes effectively local-speed, Litestream handles backup replication to Supabase object storage, `sqlite-web` provides a simple database web interface, and Supabase remains for object storage rather than primary app queries.~~ The app uses local SQLite as its primary operational database for the in-scope tables, preserving the important Supabase constraints and indexes that affect behavior. Search becomes effectively local-speed, Litestream provides backup replication to Supabase object storage, `sqlite-web` provides an internal database web interface, and Supabase remains in use for object storage rather than primary app queries.
