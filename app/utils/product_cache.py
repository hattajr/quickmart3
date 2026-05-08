"""
In-memory cache for fuzzy product search using rapidfuzz.
"""

import sqlite3
from typing import Optional

from loguru import logger
from rapidfuzz import fuzz, process, utils

from app.config import FUZZY_SEARCH_THRESHOLD
from app.db.database import get_db


class ProductSearchCache:
    """
    In-memory cache for product search data.

    Staleness is detected by comparing a single integer version counter against
    the value stored in _cache_meta. Three triggers on the products table
    (trg_products_version_insert/update/delete) increment that counter for every
    INSERT, UPDATE, and DELETE — including out-of-band edits via sqlite-web or
    direct SQL connections. This replaces the previous full-table content hash,
    making the staleness check O(1) and collision-free by construction.
    """

    def __init__(self):
        self.cache: list[tuple[int, str]] = []
        self.row_count: int = 0
        self._invalidated: bool = True
        # Cached copy of _cache_meta.search_version at last successful refresh.
        # -1 means the cache has never been loaded from the DB.
        self._version: int = -1

    def _fetch_version(self) -> Optional[int]:
        """Read the current search_version from _cache_meta, or None on DB error."""
        try:
            for conn in get_db():
                cursor = conn.cursor()
                row = cursor.execute("SELECT value FROM _cache_meta WHERE key = 'search_version'").fetchone()
                return row[0] if row else 0
        except sqlite3.Error as e:
            logger.warning(f"Cache version read failed: {e}")
        return None

    def _fetch_rows_and_version(self) -> Optional[tuple[list[tuple[int, str]], int]]:
        """Fetch all (id, searchable_text) rows and the current version in one connection.

        Reading both in the same connection ensures the version and the row snapshot
        come from the same DB state — no TOCTOU gap between version capture and
        cache population.
        """
        try:
            for conn in get_db():
                cursor = conn.cursor()
                version_row = cursor.execute("SELECT value FROM _cache_meta WHERE key = 'search_version'").fetchone()
                version = version_row[0] if version_row else 0
                cursor.execute("""
                    SELECT id, TRIM(COALESCE(name,'') || ' ' || COALESCE(keyword,'')) as searchable_text
                    FROM products
                    ORDER BY id
                """)
                rows = [(row["id"], row["searchable_text"]) for row in cursor.fetchall()]
                return rows, version
        except sqlite3.Error as e:
            logger.warning(f"Cache rows/version read failed: {e}")
        return None

    def is_stale(self) -> bool:
        """Check staleness by comparing the cached version against the live DB version.

        A single integer comparison replaces the previous full-table content hash.
        Covers all writes to products — including direct sqlite-web edits that bypass
        updated_at — because the triggers fire on every DML operation.
        """
        if self._invalidated:
            return True
        db_version = self._fetch_version()
        if db_version is None:
            return True  # DB unreachable — treat as stale
        stale = db_version != self._version
        if stale:
            logger.debug(f"Cache stale: DB version {db_version} != cached version {self._version}")
        return stale

    def refresh(self) -> bool:
        """Reload all products from the database into the cache.

        Returns True on success, False if the DB was unreachable. The caller
        must treat False as a hard failure and not serve existing stale data.
        """
        logger.info("Refreshing product search cache...")
        result = self._fetch_rows_and_version()
        if result is None:
            logger.warning("Cache refresh skipped: DB unreachable")
            return False
        rows, version = result
        self.cache = rows
        self.row_count = len(rows)
        self._version = version
        self._invalidated = False
        logger.info(f"Cache refreshed with {self.row_count} products (version {version})")
        return True

    def invalidate(self) -> None:
        """Mark cache as invalid to force refresh on next search.

        Called after admin operations so the next search skips the version check
        and reloads immediately rather than waiting for the trigger version to
        surface through a staleness check.
        """
        self._invalidated = True
        logger.debug("Product cache invalidated")

    def fuzzy_search(self, query: str, exclude_ids: Optional[list[int]] = None) -> list[int]:
        """
        Perform fuzzy search on cached products.

        Args:
            query: Search query string
            exclude_ids: Optional list of product IDs to exclude from results

        Returns:
            List of product IDs ranked by fuzzy match score
        """
        if self.is_stale():
            # refresh() returns False when the DB is unreachable regardless of
            # whether the stale flag came from invalidate() or a failed version
            # check — both paths fail closed with an empty result.
            if not self.refresh():
                logger.warning("Fuzzy search skipped: cache could not be refreshed (DB unreachable)")
                return []
        else:
            logger.debug(f"Using cached product data ({self.row_count} products)")

        if not query or not self.cache:
            return []

        # Build list of searchable items (excluding IDs if provided)
        if exclude_ids:
            exclude_set = set(exclude_ids)
            searchable = [(pid, text) for pid, text in self.cache if pid not in exclude_set]
        else:
            searchable = self.cache

        if not searchable:
            return []

        # Extract just the text for rapidfuzz
        texts = [text for _, text in searchable]
        id_map = {i: pid for i, (pid, _) in enumerate(searchable)}

        # Perform fuzzy matching
        results = process.extract(
            query,
            texts,
            scorer=fuzz.partial_ratio,
            processor=utils.default_process,
            score_cutoff=FUZZY_SEARCH_THRESHOLD,
            limit=20,  # Limit to top 20 results
        )

        # Extract product IDs in ranked order
        ranked_ids = [id_map[match[2]] for match in results]

        # Log fuzzy match results with scores
        if ranked_ids:
            match_details = [(id_map[match[2]], match[1]) for match in results]
            logger.info(f"Fuzzy search '{query}': {len(ranked_ids)} results")
            for product_id, score in match_details[:5]:  # Log top 5
                logger.debug(f"  - Product ID {product_id}: score {score:.2f}")
        else:
            logger.info(f"Fuzzy search '{query}': no results (threshold={FUZZY_SEARCH_THRESHOLD})")

        return ranked_ids


# Global singleton instance
product_cache = ProductSearchCache()
