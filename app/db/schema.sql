-- SQLite schema for quickmart. Applied once by init_db() on first startup.
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
