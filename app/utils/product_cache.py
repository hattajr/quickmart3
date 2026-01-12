"""
In-memory cache for fuzzy product search using rapidfuzz.
"""
from typing import Optional
from rapidfuzz import process, fuzz, utils
from loguru import logger

from app.config import FUZZY_SEARCH_THRESHOLD
from app.db.database import get_pg_db


class ProductSearchCache:
    """
    In-memory cache for product search data.
    Auto-refreshes when database row count changes.
    """
    
    def __init__(self):
        self.cache: list[tuple[int, str]] = []
        self.row_count: int = 0
        self._invalidated: bool = True
    
    def is_stale(self) -> bool:
        """
        Check if cache is stale by comparing row count with database.
        """
        if self._invalidated:
            return True
            
        for conn in get_pg_db():
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM products")
            current_count = cursor.fetchone()['count']
            
            is_stale = current_count != self.row_count
            if is_stale:
                logger.debug(f"Cache stale: DB has {current_count} rows, cache has {self.row_count}")
            return is_stale
    
    def refresh(self) -> None:
        """
        Reload all products from database into cache.
        Concatenates name and keyword for searchable text.
        """
        logger.info("Refreshing product search cache...")
        
        for conn in get_pg_db():
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, CONCAT_WS(' ', name, keyword) as searchable_text
                FROM products 
                ORDER BY id
            """)
            rows = cursor.fetchall()
            
            # Build cache from query results
            self.cache = [(row['id'], row['searchable_text']) for row in rows]
            
            self.row_count = len(self.cache)
            self._invalidated = False
            
            logger.info(f"Cache refreshed with {self.row_count} products")
    
    def invalidate(self) -> None:
        """
        Mark cache as invalid to force refresh on next search.
        Called after admin operations (add/edit/delete).
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
        # Refresh cache if stale
        if self.is_stale():
            self.refresh()
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
            limit=20  # Limit to top 20 results
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
        
        return ranked_ids


# Global singleton instance
product_cache = ProductSearchCache()
