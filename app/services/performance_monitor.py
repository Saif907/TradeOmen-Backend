# backend/app/services/performance_monitor.py

import asyncio
import logging
from collections import defaultdict
from typing import Dict, Any

from app.core.database import db
from app.services.metrics_engine import MetricsEngine

logger = logging.getLogger("tradeomen.monitor")

class PerformanceMonitor:
    """
    Aggregates performance stats in memory and flushes a summary every 60s.
    Zero per-request DB load.
    """
    
    # ----------------------------------------
    # IN-MEMORY STATE
    # ----------------------------------------
    _lock = asyncio.Lock()
    
    # Structure: { "GET /api/v1/trades": {"count": 10, "total_ms": 500, "errors": 0} }
    _requests: Dict[str, Dict[str, int]] = defaultdict(lambda: {"count": 0, "total_ms": 0, "errors": 0})
    
    # Cache Stats
    _cache_stats = {"hits": 0, "misses": 0}
    
    # DB Stats
    _db_stats = {"queries": 0, "slow_queries": 0}
    
    # Unique Active Users (Approximate)
    _active_users = set()

    @classmethod
    async def record_request(cls, method: str, path: str, status_code: int, duration_ms: float, user_id: str = None):
        """Called by Middleware on every request."""
        # Simple heuristic to grouping paths (avoids infinite keys for UUIDs)
        clean_path = path 
        
        key = f"{method} {clean_path}"
        
        async with cls._lock:
            stats = cls._requests[key]
            stats["count"] += 1
            stats["total_ms"] += int(duration_ms)
            if status_code >= 500:
                stats["errors"] += 1
            
            if user_id:
                cls._active_users.add(user_id)

    @classmethod
    async def record_db_query(cls, duration_ms: float, query_snippet: str):
        """Called by DB Wrapper."""
        async with cls._lock:
            cls._db_stats["queries"] += 1
            if duration_ms > 200:
                cls._db_stats["slow_queries"] += 1
                # Log slow query immediately to console (not DB) for debugging
                logger.warning(f"🐢 SLOW QUERY ({duration_ms:.2f}ms): {query_snippet[:100]}...")

    @classmethod
    async def record_auth_cache(cls, hit: bool):
        """Called by Auth Dependency."""
        async with cls._lock:
            if hit:
                cls._cache_stats["hits"] += 1
            else:
                cls._cache_stats["misses"] += 1

    @classmethod
    async def start_background_monitor(cls):
        """Background task: Writes aggregated stats to DB every 60s."""
        logger.info("🚀 Performance Monitor started.")
        while True:
            await asyncio.sleep(60)  # Sleep first, run every minute
            
            snapshot = {}
            async with cls._lock:
                # If no activity, skip writing to DB
                if not cls._requests and cls._db_stats["queries"] == 0:
                    continue 
                
                # Copy & Reset
                snapshot["requests"] = dict(cls._requests)
                snapshot["db"] = cls._db_stats.copy()
                snapshot["cache"] = cls._cache_stats.copy()
                snapshot["active_users_count"] = len(cls._active_users)
                
                # DB Pool Stats (Health Check)
                if db.pool:
                    snapshot["db_pool"] = {
                        "size": db.pool.get_size(),
                        "idle": db.pool.get_idle_size()
                    }

                # Clear counters for next minute
                cls._requests.clear()
                cls._db_stats = {"queries": 0, "slow_queries": 0}
                cls._cache_stats = {"hits": 0, "misses": 0}
                cls._active_users.clear()

            # Persist via MetricsEngine
            try:
                # 1. Add to buffer (Logs as NULL user_id)
                await MetricsEngine.log_telemetry(
                    user_id=None,
                    event_type="PERFORMANCE_REPORT",
                    category="MONITOR",
                    details=snapshot
                )
                
                # 2. FORCE FLUSH
                # Ensures data appears in DB immediately, even if traffic is low.
                await MetricsEngine.flush_telemetry()
                
                logger.info("📊 Performance metrics flushed to DB.")
            except Exception as e:
                logger.error(f"Failed to flush performance metrics: {e}")