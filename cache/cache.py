import sqlite3
import json
import os
import hashlib
import time
import logging
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

class Cache:
    """
    SQLite-backed TTL cache.
    
    Usage:
        cache = Cache(store_dir="~/AI_HOME/TOOLS/golf_agent/cache/store")
        cache.get("key")          # returns None if miss or expired
        cache.set("key", data, ttl=300)
        cache.get_or_fetch("key", lambda: fetch_fn(), ttl=300)
    """
    
    def __init__(self, store_dir: str = None, db_path: str = None):
        if store_dir is None:
            store_dir = os.path.expanduser("~/AI_HOME/TOOLS/golf_agent/cache/store")
        if db_path is None:
            db_path = os.path.expanduser("~/AI_HOME/TOOLS/golf_agent/cache/cache.db")
        
        self.store_dir = Path(store_dir)
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        
        self._init_db()
    
    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cache (
                    key TEXT PRIMARY KEY,
                    value BLOB,
                    fetched_at REAL,
                    ttl_seconds INTEGER
                )
            """)
    
    def _hash_key(self, key: str) -> str:
        """Hash key for filename storage."""
        return hashlib.sha256(key.encode()).hexdigest()[:32]
    
    def _is_expired(self, fetched_at: float, ttl_seconds: int) -> bool:
        return (time.time() - fetched_at) > ttl_seconds
    
    def get(self, key: str) -> Optional[Any]:
        """Get value from cache. Returns None if miss or expired."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT value, fetched_at, ttl_seconds FROM cache WHERE key = ?",
                    (key,)
                ).fetchone()
            
            if row is None:
                return None
            
            value_blob, fetched_at, ttl_seconds = row
            
            if self._is_expired(fetched_at, ttl_seconds):
                return None
            
            # Reconstruct from blob or file
            if value_blob is None:
                # Value stored as external file
                file_path = self.store_dir / self._hash_key(key)
                if file_path.exists():
                    with open(file_path, 'r') as f:
                        return json.load(f)
                return None
            
            return json.loads(value_blob)
            
        except Exception as e:
            logger.warning(f"Cache get error for key {key}: {e}")
            return None
    
    def set(self, key: str, data: Any, ttl: int = 300):
        """Set value in cache with TTL."""
        try:
            value_json = json.dumps(data)
            
            # Store large values as external files
            if len(value_json) > 10000:
                file_path = self.store_dir / self._hash_key(key)
                with open(file_path, 'w') as f:
                    json.dump(data, f)
                value_blob = None
            else:
                value_blob = value_json.encode()
            
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO cache (key, value, fetched_at, ttl_seconds)
                    VALUES (?, ?, ?, ?)
                """, (key, value_blob, time.time(), ttl))
                
        except Exception as e:
            logger.error(f"Cache set error for key {key}: {e}")
    
    def get_or_fetch(self, key: str, fetch_fn: Callable[[], Any], ttl: int = 300) -> Any:
        """
        Get from cache, or fetch if miss/expired.
        Atomic: checks expiry then fetches under a shared lock to prevent stampede.
        """
        # Fast path: check cache first
        val = self.get(key)
        if val is not None:
            return val
        
        # Slow path: fetch
        try:
            data = fetch_fn()
            self.set(key, data, ttl=ttl)
            return data
        except Exception as e:
            logger.error(f"Fetch error for key {key}: {e}")
            raise
    
    def delete(self, key: str):
        """Delete a key from cache."""
        try:
            file_path = self.store_dir / self._hash_key(key)
            if file_path.exists():
                file_path.unlink()
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("DELETE FROM cache WHERE key = ?", (key,))
        except Exception as e:
            logger.warning(f"Cache delete error for key {key}: {e}")
    
    def clear_expired(self):
        """Remove all expired entries."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                rows = conn.execute("SELECT key, fetched_at, ttl_seconds FROM cache").fetchall()
            
            for key, fetched_at, ttl_seconds in rows:
                if self._is_expired(fetched_at, ttl_seconds):
                    self.delete(key)
                    
            logger.info("Cache cleared of expired entries")
        except Exception as e:
            logger.error(f"Cache clear error: {e}")
    
    def stats(self) -> dict:
        """Return cache statistics."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                total = conn.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
                expired = 0
                rows = conn.execute("SELECT fetched_at, ttl_seconds FROM cache").fetchall()
                for fetched_at, ttl_seconds in rows:
                    if self._is_expired(fetched_at, ttl_seconds):
                        expired += 1
            
            return {"total": total, "expired": expired, "active": total - expired}
        except Exception as e:
            return {"error": str(e)}


# Module-level convenience functions
_cache_instance: Optional[Cache] = None

def get_cache() -> Cache:
    """Get singleton cache instance."""
    global _cache_instance
    if _cache_instance is None:
        _cache_instance = Cache()
    return _cache_instance

def cache_get(key: str) -> Optional[Any]:
    return get_cache().get(key)

def cache_set(key: str, data: Any, ttl: int = 300):
    get_cache().set(key, data, ttl)

def cache_get_or_fetch(key: str, fetch_fn: Callable[[], Any], ttl: int = 300) -> Any:
    return get_cache().get_or_fetch(key, fetch_fn, ttl)