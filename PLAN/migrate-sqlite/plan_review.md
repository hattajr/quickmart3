# Plan Review

### Round 1

Q1: The plan says Docker Compose should start `litestream` first and have it `restore` before `replicate`, but it does not define how the `app` service waits until restore is actually complete before opening SQLite. `depends_on` alone does not guarantee readiness. What exact readiness gate or startup sequence prevents the app from starting against an empty volume before restore finishes?
Owner: [Planner]
Status: [SOLVED]
A: Use a one-shot init container pattern. A `restore` service runs `litestream restore -if-replica-exists -if-db-not-exists /data/quickmart.sqlite3` and exits 0 on success. The `app` and `litestream` replicate services both use `depends_on: { restore: { condition: service_completed_successfully } }`. The shared named volume guarantees the restored file is visible to all services. This removes any race because Compose blocks `app` start until the init container exits cleanly. -- by Planner

Q2: The plan replaces `id = ANY(%s)` with `id IN (?, ?, ...)`, but that SQL does not preserve ranked order. The draft’s goal depends on search behavior staying correct, not just fast. Where in the plan is the result-order preservation step specified so the final response still follows the fuzzy-search ranking rather than SQLite’s arbitrary row order?
Owner: [Planner]
Status: [SOLVED]
A: Order preservation is already handled in application code at [app/routes/products.py](app/routes/products.py#L138-L143) via `row_map` and the `ordered_results = [row_map[pid] for pid in ranked_ids if pid in row_map]` reorder step. The Builder must keep that reorder block intact when changing the SQL. The Builder checklist is updated to call this out explicitly. -- by Planner

Q3: The draft says to preserve important constraints and relationships from Supabase, and it explicitly includes `categories` because `products.category_id` exists in the real schema. However, the SQLite DDL in the plan does not define a foreign key from `products.category_id` to `categories.id`, nor any decision about `categories.parent_id`. Is the plan intentionally dropping those relationships, or should the schema preserve them?
Owner: [Planner]
Status: [SOLVED]
A: Preserve them. The SQLite DDL is updated to add `FOREIGN KEY (category_id) REFERENCES categories(id)` on `products` and `FOREIGN KEY (parent_id) REFERENCES categories(id)` on `categories` (self-reference). Both use default `ON DELETE NO ACTION` to mirror Supabase behavior. -- by Planner

Q4: The import plan says to use `INSERT OR IGNORE` for idempotency, but it never states whether original primary key values from Supabase must be preserved explicitly during import. That is critical for `categories.id`, `products.id`, and foreign-keyed `sold_items.item_id`. Please specify whether IDs are copied as-is, how sequence state is handled afterward, and whether silent `IGNORE` conflicts are acceptable for primary-key collisions.
Owner: [Planner]
Status: [SOLVED]
A: Primary keys are copied as-is so existing FK relationships stay valid. After import, the script updates `sqlite_sequence` for each AUTOINCREMENT table to `MAX(id)` so future inserts get unique IDs. The first import must run against an empty SQLite DB; any PK collision in that case is an error and must abort with a non-zero exit. `INSERT OR IGNORE` is only acceptable on re-runs of the same import. -- by Planner

Q5: The plan adds a foreign key from `sold_items.item_id` to `products.id`, but it does not address the migration risk if historical `sold_items` rows reference products that are no longer present in `products`. In that case the import will fail or silently drop rows depending on implementation. What is the intended behavior for orphaned historical references during import?
Owner: [Planner]
Status: [SOLVED]
A: The import script disables foreign keys for the duration of the data load via `PRAGMA foreign_keys = OFF` so historical orphans are preserved. After the load, it runs `PRAGMA foreign_key_check` and logs any orphan rows for visibility but does not delete them. Foreign keys are re-enabled on every normal app connection through the runtime PRAGMA block, so new inserts are still constrained. -- by Planner

Q6: The draft calls out that Litestream must target Supabase’s S3-compatible endpoint, not AWS defaults. The plan uses `${S3_ENDPOINT_URL}` in `litestream.yml`, but it does not specify the exact endpoint shape required for Litestream versus the public object URL used by the app for images. What exact env var value format is required here so Builder does not accidentally wire Litestream to the wrong Supabase URL?
Owner: [Planner]
Status: [SOLVED]
A: Litestream uses the same `S3_ENDPOINT_URL` value the app already uses for Supabase S3 (form: `https://<project_id>.storage.supabase.co/storage/v1/s3`). The backup bucket is dedicated and separate from the image bucket: `sqlite-backup`. The plan sets `bucket: sqlite-backup` directly (not `${S3_BUCKET_NAME}`, which is the image bucket) and keeps `endpoint: ${S3_ENDPOINT_URL}`, `region: ${S3_REGION}`, and `force-path-style: true`. -- by Planner

### Round 2

Q7: The draft’s deployment shape says the SQLite database directory lives under `./app/db/database/` as repo-local runtime data on a mounted Docker volume. The plan’s Docker Compose section now standardizes the runtime path to `/data/quickmart.sqlite3` inside containers. Those are not the same layout, and Builder could reasonably implement either. Which path is canonical for production Compose, and how should it relate to the repo-local `app/db/database/` directory promised in the draft?
Owner: [Planner]
Status: [SOLVED]
A: Canonical path is `./data/` at the repo root, bind-mounted to `/data/` in every container. The DB file is `./data/quickmart.sqlite3` on host and `/data/quickmart.sqlite3` in containers. The earlier `app/db/database/` location is dropped in favor of this simpler, single path. The plan and `.gitignore` are updated accordingly. -- by Planner

Q8: The QA checklist still uses the old Litestream path in `litestream snapshots /app/app/db/database/quickmart.sqlite3`, but the plan’s `litestream.yml` and Compose sections now use `/data/quickmart.sqlite3`. Which one is correct? This needs one canonical path or QA will validate the wrong file.
Owner: [Planner]
Status: [SOLVED]
A: Resolved by Q7. Canonical path is `/data/quickmart.sqlite3` in containers, `./data/quickmart.sqlite3` on host. The QA command is updated to use this path. -- by Planner

Q9: The plan now sets `sync-interval: 1h` and `snapshot-interval: 24h`, but the QA checklist still says Litestream replication should be visible “within a few minutes of writes.” That verification step no longer matches the configured backup cadence and could fail by design. What exact QA expectation should replace it?
Owner: [Planner]
Status: [SOLVED]
A: Replaced. New QA step: “Within ~70 minutes of a write (`sync-interval: 1h` plus a small buffer), `litestream snapshots /data/quickmart.sqlite3` lists at least one snapshot in the `sqlite-backup` bucket. For faster manual validation, run `litestream replicate --once` (or restart the litestream service) and verify a snapshot appears.” -- by Planner

### Round 3

Q10: The draft and plan still disagree on the canonical SQLite storage path. [draft.md](PLAN/migrate-sqlite/draft.md) still says the deployment shape uses a repo-local database directory under `./app/db/database/`, while [plan.md](PLAN/migrate-sqlite/plan.md) now standardizes on `./data/quickmart.sqlite3` and `./data/` bind mounts. This is a direct cross-document inconsistency on a core deployment detail. Which location is final, and the other document must be updated to match.
Owner: [Planner]
Status: [SOLVED]
A: Final canonical path is `./data/` at the repo root. [draft.md](PLAN/migrate-sqlite/draft.md) is updated to match. -- by Planner

Q11: [plan.md](PLAN/migrate-sqlite/plan.md) is internally inconsistent about the Litestream replica path. The `litestream.yml` section sets replica `path: quickmart`, but the cutover step says to confirm replication at `litestream/quickmart`. Which object prefix is correct? This should be one value throughout the plan so backup verification and restore procedures target the same location.
Owner: [Planner]
Status: [SOLVED]
A: Canonical replica path is `litestream/quickmart` (within the `sqlite-backup` bucket). `litestream.yml` and any other references in the plan are updated to match. -- by Planner

## Plan approved
