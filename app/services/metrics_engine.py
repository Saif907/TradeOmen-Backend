# backend/app/services/metrics_engine.py
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
from app.core.database import db

logger = logging.getLogger(__name__)

class MetricsEngine:
    """
    Central engine for tracking User Behavior, System Health, and Engagement Metrics.
    Separated from QuotaManager (Limits) to focus on Analytics (Insights).
    """

    @staticmethod
    async def log_telemetry(
        user_id: str, 
        event_type: str, 
        category: str = "INFO", 
        details: Dict[str, Any] = None,
        path: str = None
    ):
        """
        Records a specific user event for future analysis.
        Examples: 'SYNC_ERROR', 'IMPORT_SUCCESS', 'STRATEGY_CREATED'
        """
        if not db.pool: return
        
        try:
            query = """
                INSERT INTO public.user_events (user_id, event_type, category, details, path)
                VALUES ($1, $2, $3, $4, $5)
            """
            # Also update last_active_at on any event
            update_active_query = "UPDATE public.user_profiles SET last_active_at = NOW() WHERE id = $1"
            
            async with db.pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(query, user_id, event_type, category, details or {}, path)
                    await conn.execute(update_active_query, user_id)
                    
        except Exception as e:
            # Never crash the app due to analytics failure
            logger.error(f"Telemetry log failed: {e}")

    @staticmethod
    async def get_user_insights(user_id: str) -> Dict[str, Any]:
        """
        Compiles a 360-degree view of the user's interaction with the platform.
        Used for: 'User Health' dashboard or internal admin view.
        """
        if not db.pool: return {}

        insights = {
            "engagement": {},
            "behavior": {},
            "issues": {}
        }

        # 1. Engagement Metrics (Activity Streak)
        try:
            profile_q = "SELECT last_active_at, created_at FROM public.user_profiles WHERE id = $1"
            profile = await db.fetch_one(profile_q, user_id)
            if profile:
                last_active = profile["last_active_at"]
                days_since_active = (datetime.now(last_active.tzinfo) - last_active).days if last_active else 999
                insights["engagement"]["days_since_active"] = days_since_active
                insights["engagement"]["status"] = "CHURNED" if days_since_active > 14 else "ACTIVE"
        except Exception: 
            pass

        # 2. Behavior Persona (Manual vs Sync)
        try:
            # Check ratio of manual vs synced trades
            trade_q = """
                SELECT 
                    COUNT(*) as total,
                    COUNT(CASE WHEN source_type = 'AUTO_SYNC' THEN 1 END) as synced
                FROM trades WHERE user_id = $1
            """
            trade_stats = await db.fetch_one(trade_q, user_id)
            total = trade_stats["total"] or 0
            synced = trade_stats["synced"] or 0
            
            persona = "NEWBIE"
            if total > 0:
                sync_ratio = synced / total
                if sync_ratio > 0.8: persona = "ALGO_TRADER"
                elif sync_ratio < 0.2: persona = "MANUAL_JOURNALER"
                else: persona = "HYBRID_TRADER"
            
            insights["behavior"]["persona"] = persona
            insights["behavior"]["total_trades_logged"] = total
        except Exception:
            pass

        # 3. Issue Tracking (Recent Errors)
        try:
            error_q = """
                SELECT event_type, COUNT(*) as count 
                FROM user_events 
                WHERE user_id = $1 AND category IN ('ERROR', 'CRITICAL') 
                  AND created_at > NOW() - INTERVAL '7 days'
                GROUP BY event_type
            """
            errors = await db.fetch_all(error_q, user_id)
            insights["issues"]["recent_errors"] = {row["event_type"]: row["count"] for row in errors}
            
            # Health Score Calculation
            error_count = sum(row["count"] for row in errors)
            health_score = 100
            health_score -= (error_count * 5) # Deduct 5 points per error
            health_score -= (insights["engagement"].get("days_since_active", 0) * 2) # Deduct for inactivity
            insights["health_score"] = max(0, health_score)
            
        except Exception:
            pass

        return insights