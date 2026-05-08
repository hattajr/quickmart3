# QA

### Round 1

- [x] Template-rendering routes still use Starlette's deprecated `TemplateResponse(name, context)` call shape, which emits `DeprecationWarning` today and will become a framework-compatibility break on upgrade. The failure is reproduced through `/admin/verify` and `/catalog`, and the same pattern appears in other template routes. (`app/routes/admin.py` line 52, `app/routes/products.py` line 200)
  Owner: [Builder]
  Test: `tests/test_sqlite_integration.py::test_template_routes_do_not_emit_deprecation_warnings`
  - Fixed all deprecated `TemplateResponse(name, {"request": request, ...})` calls across `app/routes/admin.py` (13 call sites), `app/routes/products.py` (2 call sites), and `app/routes/main.py` (3 call sites). Each was converted to `TemplateResponse(request, name, {...without "request" key...})`. All 7 tests now pass. — by Builder

### Round 2

- [x] Public search results interpolate product names directly into an HTML string, so a malicious product name is rendered as executable markup instead of escaped text. This is a stored XSS path on `/search`: an admin can create a product whose name contains HTML or JavaScript, and every shopper who searches for it receives attacker-controlled markup in the response body. (`app/routes/products.py` line 145-180)
  Owner: [Builder]
  Test: `tests/test_sqlite_integration.py::test_search_results_escape_product_names`
  - Fixed. Added `import html` to `app/routes/products.py` and replaced `{row["name"]}` with `{html.escape(row["name"])}` in the f-string block. All 8 tests now pass. — by Builder

### Round 3

- [x] `ProductSearchCache` still misses some direct sqlite-web edits because the new MD5 fingerprint serializes rows as a delimiter-unsafe `"id:text|id:text"` string before hashing. Two coordinated name changes can produce the same serialized payload with different underlying searchable text, so `is_stale()` returns `False` and `/search` continues serving stale cached matches. The failure is reproduced by changing product names from `a|2:b` / `c` to `a` / `b|2:c` without touching `updated_at`; the cache incorrectly reports fresh after the edit. (`app/utils/product_cache.py` line 28-31, `app/utils/product_cache.py` line 44-57)
  Owner: [Builder]
  Test: `tests/test_sqlite_integration.py::test_product_cache_detects_direct_sqlite_edits_with_delimiter_ambiguous_text`
  - Fixed. Replaced the `"|".join(f"{pid}:{text}" ...)` single-string serialization in `_fingerprint_from_rows()` with a per-row `hashlib.md5().update()` approach using null-byte delimiters — each row contributes `f"{pid}\x00{text}\x00"` to the running digest. Null bytes never appear in product names or keywords, so no two distinct row-content distributions can produce the same fingerprint. All 9 integration tests pass. — by Builder

### Round 4

- [x] `ProductSearchCache` still does not fail closed on actual SQLite read errors. `_fetch_version()` and `_fetch_rows_and_version()` are documented as returning `None` on DB error, but they do not catch `sqlite3.Error`; `get_db()` logs and re-raises instead. After the cache is warmed, if the SQLite file disappears, `/search` returns HTTP 500 instead of the intended empty-results response. (`app/utils/product_cache.py` line 34, `app/utils/product_cache.py` line 42, `app/utils/product_cache.py` line 62, `app/utils/product_cache.py` line 79, `app/utils/product_cache.py` line 108)
  Owner: [Builder]
  Test: `tests/test_sqlite_integration.py::test_search_fails_closed_when_sqlite_becomes_unreadable_after_cache_warmup`
  - Fixed. Added `import sqlite3` to `app/utils/product_cache.py`. Wrapped the body of both `_fetch_version()` and `_fetch_rows_and_version()` in `try/except sqlite3.Error` that logs a warning and returns `None`. `get_db()` still re-raises on the database layer; the catch now lives in the cache methods that contract to return `None` on failure, so `refresh()` returns `False` and `fuzzy_search()` returns `[]` instead of propagating a 500. All 10 tests pass. — by Builder

### Round 5

- [ ] `/finish` reports `200 OK` even when checkout persistence fails. A cart containing a product ID that no longer exists reaches `finish_checkout()`, gets scheduled as a background success path, then `process_checkout()` hits `FOREIGN KEY constraint failed` and logs the error after the response has already told the caller the checkout succeeded. No `sold_items` row or `sold_sessions` row is written, so the purchase is silently dropped while the client sees success. (`app/routes/main.py` line 194, `app/routes/main.py` line 222)
  Owner: [Builder]
  Test: `tests/test_sqlite_integration.py::test_checkout_rejects_carts_with_missing_products`
  - Fixed. Added product ID validation in `finish_checkout()` before scheduling the background task. All unique cart item IDs are checked against the `products` table using a single `SELECT COUNT(*)` query; if any are missing, the handler returns `400` immediately without writing anything and without scheduling `process_checkout`. Valid carts continue through the existing `BackgroundTasks` path unchanged. All 11 tests pass. — by Builder

## Test Run

Ran `uv run pytest -v`

Result: 11 passed

Covered critical behaviors: cold start schema creation, WAL + foreign key enforcement, root page session initialization, admin verify/search/duplicate handling, admin edit/delete cache invalidation, catalog rendering, ranked search ordering, search selection logging, checkout batch writes with sold-session dedupe, feedback persistence, HTML escaping in public search results, direct sqlite-web style cache invalidation after out-of-band product edits, fail-closed behavior when SQLite becomes unreadable after cache warm-up, and rejection of checkout carts whose product IDs no longer exist.

Not exercised here: Litestream replication/restore, sqlite-web network binding, and container restart persistence. Those require live container infrastructure and remain QA cutover checks from the plan.