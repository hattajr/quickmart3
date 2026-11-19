-- SQLite schema for quickmart. Applied by init_db() on every startup; all DDL is idempotent.
-- Foreign keys must be enabled per-connection (PRAGMA foreign_keys = ON).

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    parent_id INTEGER NULL,
    FOREIGN KEY (parent_id) REFERENCES categories(id)
);
CREATE INDEX IF NOT EXISTS categories_index_0 ON categories(name);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    barcode TEXT NULL,
    name TEXT NOT NULL UNIQUE,
    brand TEXT NULL,
    price INTEGER NOT NULL DEFAULT 1,
    unit TEXT NULL,
    stock INTEGER DEFAULT 0,
    description TEXT NULL,
    category_id INTEGER NULL,
    keyword TEXT NULL,
    image_url TEXT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    purchase_price INTEGER NULL,
    latest_price INTEGER NULL,
    FOREIGN KEY (category_id) REFERENCES categories(id)
);
CREATE INDEX IF NOT EXISTS idx_products_category_id ON products(category_id);
CREATE INDEX IF NOT EXISTS products_index_1 ON products(name);
CREATE INDEX IF NOT EXISTS products_index_2 ON products(barcode);

-- Cache version counter. Incremented by triggers for every products write,
-- including out-of-band edits via sqlite-web or direct SQL connections.
CREATE TABLE IF NOT EXISTS _cache_meta (
    key TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO _cache_meta (key, value) VALUES ('search_version', 0);

CREATE TRIGGER IF NOT EXISTS trg_products_version_insert
    AFTER INSERT ON products
BEGIN
    UPDATE _cache_meta SET value = value + 1 WHERE key = 'search_version';
END;

CREATE TRIGGER IF NOT EXISTS trg_products_version_update
    AFTER UPDATE ON products
BEGIN
    UPDATE _cache_meta SET value = value + 1 WHERE key = 'search_version';
END;

CREATE TRIGGER IF NOT EXISTS trg_products_version_delete
    AFTER DELETE ON products
BEGIN
    UPDATE _cache_meta SET value = value + 1 WHERE key = 'search_version';
END;

CREATE TABLE IF NOT EXISTS sold_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL UNIQUE,
    ip_address TEXT NULL,
    user_agent TEXT NULL,
    device_type TEXT NULL,
    browser TEXT NULL,
    os TEXT NULL,
    country TEXT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_sold_sessions_country ON sold_sessions(country);
CREATE INDEX IF NOT EXISTS idx_sold_sessions_created_at ON sold_sessions(created_at);
CREATE INDEX IF NOT EXISTS idx_sold_sessions_ip_address ON sold_sessions(ip_address);
CREATE INDEX IF NOT EXISTS idx_sold_sessions_session_id ON sold_sessions(session_id);

CREATE TABLE IF NOT EXISTS sold_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    item_id INTEGER NOT NULL,
    item_name TEXT NOT NULL,
    price_at_purchase REAL NOT NULL,
    quantity INTEGER NOT NULL,
    total_price REAL NOT NULL,
    timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (item_id) REFERENCES products(id)
);
CREATE INDEX IF NOT EXISTS idx_sold_items_item_id ON sold_items(item_id);
CREATE INDEX IF NOT EXISTS idx_sold_items_session_id ON sold_items(session_id);

CREATE TABLE IF NOT EXISTS search_selections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NULL,
    product_id INTEGER NOT NULL,
    product_name TEXT NULL,
    search_query TEXT NULL,
    ip_address TEXT NULL,
    selected_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_search_selections_selected_at ON search_selections(selected_at);

CREATE TABLE IF NOT EXISTS feedback_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    message TEXT NOT NULL,
    ip_address TEXT NULL,
    user_agent TEXT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    is_deleted TEXT DEFAULT 'N' CHECK (is_deleted IN ('Y', 'N'))
);
CREATE INDEX IF NOT EXISTS idx_fm_created_at ON feedback_messages(created_at);
CREATE INDEX IF NOT EXISTS idx_fm_ip_address ON feedback_messages(ip_address);
CREATE INDEX IF NOT EXISTS idx_fm_is_deleted ON feedback_messages(is_deleted);
CREATE INDEX IF NOT EXISTS idx_fm_session_id ON feedback_messages(session_id);

-- Supplier invoices parsed from weekly photos by app.services.supplier_invoices.
-- Kept apart from the shop's products table on purpose: these record what suppliers
-- charge, keyed by the product's original (Latin) name.
CREATE TABLE IF NOT EXISTS supplier_invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier TEXT NOT NULL,
    invoice_date TEXT NOT NULL,
    invoice_no TEXT NOT NULL,
    document_type TEXT NOT NULL,
    total_qty INTEGER NOT NULL,
    total_amount INTEGER NOT NULL,
    page_count INTEGER NOT NULL CHECK (page_count >= 1),
    model TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (supplier, invoice_date)
);

-- One row per photo. image_jpeg is the upright photo kept for double-checking the parse;
-- raw_json is the model's answer for that page.
CREATE TABLE IF NOT EXISTS supplier_invoice_pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER NOT NULL,
    page_no INTEGER NOT NULL CHECK (page_no >= 1),
    source_file TEXT NOT NULL,
    source_sha256 TEXT NOT NULL UNIQUE,
    rotation_deg INTEGER NOT NULL CHECK (rotation_deg IN (0, 90, 180, 270)),
    image_jpeg BLOB NOT NULL,
    image_width_px INTEGER NOT NULL,
    image_height_px INTEGER NOT NULL,
    raw_json TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (invoice_id) REFERENCES supplier_invoices(id) ON DELETE CASCADE,
    UNIQUE (invoice_id, page_no)
);

-- name_latin is the product's identity across invoices. Rename it here to fix it on every
-- invoice, or repoint supplier_invoice_items.product_id to merge two spellings.
CREATE TABLE IF NOT EXISTS supplier_products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier TEXT NOT NULL,
    name_latin TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (supplier, name_latin)
);

CREATE TABLE IF NOT EXISTS supplier_invoice_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    page_no INTEGER NOT NULL,
    line_no INTEGER NOT NULL,
    product_name TEXT NOT NULL,
    items_per_box TEXT NOT NULL,
    box TEXT NOT NULL,
    qty INTEGER NOT NULL,
    unit_price INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    note TEXT NOT NULL,
    struck_out INTEGER NOT NULL DEFAULT 0 CHECK (struck_out IN (0, 1)),
    FOREIGN KEY (invoice_id) REFERENCES supplier_invoices(id) ON DELETE CASCADE,
    FOREIGN KEY (product_id) REFERENCES supplier_products(id),
    UNIQUE (invoice_id, page_no, line_no)
);
CREATE INDEX IF NOT EXISTS idx_supplier_invoice_items_product_id ON supplier_invoice_items(product_id);

-- Unit price per product over time. Crossed-out and free (0 won) rows say nothing about price.
CREATE VIEW IF NOT EXISTS supplier_price_history AS
SELECT
    p.id AS product_id,
    p.supplier,
    p.name_latin,
    i.product_name,
    v.invoice_date,
    i.unit_price,
    LAG(i.unit_price) OVER w AS prev_unit_price,
    i.unit_price - LAG(i.unit_price) OVER w AS change
FROM supplier_invoice_items i
JOIN supplier_invoices v ON v.id = i.invoice_id
JOIN supplier_products p ON p.id = i.product_id
WHERE i.struck_out = 0 AND i.unit_price > 0
WINDOW w AS (PARTITION BY p.id ORDER BY v.invoice_date);
