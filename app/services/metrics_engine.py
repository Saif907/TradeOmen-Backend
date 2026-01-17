# backend/app/services/metrics_engine.py

import logging
import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP, getcontext
from typing import Dict, Any, List, Optional

from app.core.database import db

logger = logging.getLogger("tradeomen.metrics")

# Use Decimal precision suitable for money micro-charges
getcontext().prec = 12

PRICING_TABLE = {
    "gpt-4.1": {"input_per_m1": Decimal("2.00"), "output_per_m1": Decimal("8.00")},
    "gpt-5": {"input_per_m1": Decimal("1.25"), "output_per_m1": Decimal("10.00")},
    "default": {"input_per_m1": Decimal("1.00"), "output_per_m1": Decimal("3.00")},
}

class MetricsEngine:
    # ------------------------------------------------------------------
    # OPTIMIZATION CONFIG: Batch Buffering
    # ------------------------------------------------------------------
    BATCH_SIZE = 20  # Flush after this many logs
    
    _AI_LOG_BUFFER: List[tuple] = []
    _TELEMETRY_BUFFER: List[tuple] = []
    
    # Locks to prevent race conditions during async flushes
    _ai_lock = asyncio.Lock()
    _telemetry_lock = asyncio.Lock()

    @staticmethod
    def _calculate_cost(model: str, input_tokens: int, output_tokens: int) -> Decimal:
        """
        Calculate estimated cost in USD using Decimal for safety.
        """
        rates = PRICING_TABLE.get(model, PRICING_TABLE["default"])
        cost_input = (Decimal(input_tokens) / Decimal(1_000_000)) * rates["input_per_m1"]
        cost_output = (Decimal(output_tokens) / Decimal(1_000_000)) * rates["output_per_m1"]
        total = (cost_input + cost_output).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        return total

    @classmethod
    async def log_ai_usage(
        cls,
        user_id: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        latency_ms: float,
        provider: str,
        context: str = "chat",
    ):
        """
        Buffers AI usage logs and flushes when batch size is reached.
        """
        # Guard inputs
        input_tokens = max(0, int(input_tokens))
        output_tokens = max(0, int(output_tokens))
        latency_ms = float(latency_ms)
        est_cost = cls._calculate_cost(model, input_tokens, output_tokens)
        
        # Prepare Data Tuple (matches DB schema order)
        # Schema: user_id, model, provider, input_tokens, output_tokens, est_cost, latency_ms, context, created_at
        log_entry = (
            user_id,
            model,
            provider,
            input_tokens,
            output_tokens,
            float(est_cost), # asyncpg handles float nicely for numeric columns too
            latency_ms,
            context,
            datetime.now(timezone.utc)
        )

        # Add to Buffer safely
        cls._AI_LOG_BUFFER.append(log_entry)

        # Check if flush is needed
        if len(cls._AI_LOG_BUFFER) >= cls.BATCH_SIZE:
            await cls.flush_ai_logs()

    @classmethod
    async def flush_ai_logs(cls):
        """
        Writes buffered AI logs to DB in one single transaction (Bulk Insert).
        """
        async with cls._ai_lock:
            if not cls._AI_LOG_BUFFER:
                return
            
            # Swap buffer
            batch_to_insert = list(cls._AI_LOG_BUFFER)
            cls._AI_LOG_BUFFER.clear()

        if not db.pool:
            logger.warning(f"Dropping {len(batch_to_insert)} AI logs (DB unavailable)")
            return

        query = """
            INSERT INTO public.ai_usage_logs
            (user_id, model, provider, input_tokens, output_tokens, est_cost, latency_ms, context, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        """

        try:
            async with db.pool.acquire() as conn:
                await conn.executemany(query, batch_to_insert)
        except Exception:
            logger.exception("Failed to flush AI usage logs")

    @classmethod
    async def log_telemetry(
        cls,
        user_id: str,
        event_type: str,
        category: str = "INFO",
        details: Dict[str, Any] = None,
        path: str = None,
    ):
        """
        Buffers telemetry events and flushes in batches.
        """
        # Prepare Data Tuple
        # Schema: user_id, event_type, category, details, path, created_at
        entry = (
            user_id,
            event_type,
            category,
            json.dumps(details) if details else "{}",
            path,
            datetime.now(timezone.utc)
        )

        cls._TELEMETRY_BUFFER.append(entry)

        if len(cls._TELEMETRY_BUFFER) >= cls.BATCH_SIZE:
            await cls.flush_telemetry()

    @classmethod
    async def flush_telemetry(cls):
        """
        Writes buffered telemetry and efficiently updates 'last_active_at'.
        """
        async with cls._telemetry_lock:
            if not cls._TELEMETRY_BUFFER:
                return
            
            batch = list(cls._TELEMETRY_BUFFER)
            cls._TELEMETRY_BUFFER.clear()

        if not db.pool:
            return

        try:
            insert_query = """
                INSERT INTO public.user_events (user_id, event_type, category, details, path, created_at)
                VALUES ($1, $2, $3, $4::jsonb, $5, $6)
            """

            # ✅ FIX: Filter out None values.
            # Only update user_profiles for real users (user_id is not None)
            unique_users = list(set(row[0] for row in batch if row[0]))
            
            update_active_query = """
                UPDATE public.user_profiles 
                SET last_active_at = NOW() 
                WHERE id = ANY($1::uuid[])
            """

            async with db.pool.acquire() as conn:
                async with conn.transaction():
                    # 1. Bulk Insert Events (Postgres accepts NULL user_id for system events)
                    await conn.executemany(insert_query, batch)
                    
                    # 2. Batch Update 'last_active_at' for all REAL users
                    if unique_users:
                        await conn.execute(update_active_query, unique_users)

        except Exception:
            logger.exception("Failed to flush telemetry logs")

    @classmethod
    async def force_flush_all(cls):
        """
        Call this on application shutdown to save remaining logs.
        """
        logger.info("Force flushing metrics buffers...")
        await cls.flush_ai_logs()
        await cls.flush_telemetry()

    # ------------------------------------------------------------------
    # READ-ONLY ANALYTICS (Unchanged mostly, just added safety checks)
    # ------------------------------------------------------------------

    @staticmethod
    async def get_ai_spend_analytics(user_id: str, days: int = 30) -> Dict[str, Any]:
        if not db.pool:
            return {}
        
        days = 30 if days <= 0 else days

        try:
            query = """
                SELECT
                    model,
                    COUNT(*) AS requests,
                    SUM(input_tokens) AS total_input,
                    SUM(output_tokens) AS total_output,
                    SUM(est_cost) AS total_cost,
                    AVG(latency_ms) AS avg_latency
                FROM public.ai_usage_logs
                WHERE user_id = $1
                  AND created_at >= NOW() - ($2 * INTERVAL '1 day')
                GROUP BY model
            """
            rows = await db.fetch_all(query, user_id, days)
            breakdown = [dict(r) for r in rows]
            total_spend = sum((r.get("total_cost") or 0) for r in breakdown)
            return {
                "period": f"last_{days}_days",
                "total_spend_usd": round(float(total_spend), 6),
                "model_breakdown": breakdown,
            }
        except Exception:
            logger.exception("Failed to compute AI spend analytics", extra={"user_id": user_id})
            return {"info": "No AI usage data available yet."}

    @staticmethod
    async def get_user_insights(user_id: str) -> Dict[str, Any]:
        if not db.pool:
            return {}

        insights = {"engagement": {"status": "UNKNOWN"}, "behavior": {"persona": "NEWBIE"}, "health_score": 100}
        try:
            profile = await db.fetch_one("SELECT last_active_at FROM public.user_profiles WHERE id = $1", user_id)
            if profile and profile["last_active_at"]:
                last_active = profile["last_active_at"]
                if not last_active.tzinfo:
                    last_active = last_active.replace(tzinfo=timezone.utc)
                
                days_inactive = (datetime.now(timezone.utc) - last_active).days
                status = "ACTIVE"
                if days_inactive > 30:
                    status = "CHURNED"
                elif days_inactive > 14:
                    status = "AT_RISK"
                insights["engagement"] = {"days_since_active": days_inactive, "status": status}

            stats = await db.fetch_one(
                """
                SELECT
                  COUNT(*) as total,
                  COUNT(*) FILTER (WHERE source_type = 'AUTO_SYNC') as synced
                FROM public.trades
                WHERE user_id = $1
                """,
                user_id,
            )
            if stats and stats["total"] and stats["total"] > 5:
                total = stats["total"]
                synced = stats["synced"] or 0
                ratio = synced / total
                if ratio > 0.8:
                    persona = "ALGO_TRADER"
                elif ratio < 0.2:
                    persona = "MANUAL_JOURNALER"
                else:
                    persona = "HYBRID_TRADER"
                insights["behavior"]["persona"] = persona

        except Exception:
            logger.exception("Error generating insights", extra={"user_id": user_id})

        return insights