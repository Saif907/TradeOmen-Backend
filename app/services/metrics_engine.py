# backend/app/services/metrics_engine.py (excerpt, corrected)

import logging
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP, getcontext
from typing import Dict, Any, List, Optional

from app.core.database import db

logger = logging.getLogger("tradeomen.metrics")

# Use Decimal precision suitable for money micro-charges
getcontext().prec = 12

PRICING_TABLE = {
    # example entries
    "gpt-4.1": {"input_per_m1": Decimal("2.00"), "output_per_m1": Decimal("8.00")},
    "gpt-5": {"input_per_m1": Decimal("1.25"), "output_per_m1": Decimal("10.00")},
    "default": {"input_per_m1": Decimal("1.00"), "output_per_m1": Decimal("3.00")},
}


class MetricsEngine:
    @staticmethod
    def _calculate_cost(model: str, input_tokens: int, output_tokens: int) -> Decimal:
        """
        Calculate estimated cost in USD using Decimal for safety.
        Pricing table keys: input_per_m1, output_per_m1 (per 1,000,000 tokens).
        """
        rates = PRICING_TABLE.get(model, PRICING_TABLE["default"])
        # cost = (tokens / 1_000_000) * rate_per_m1
        cost_input = (Decimal(input_tokens) / Decimal(1_000_000)) * rates["input_per_m1"]
        cost_output = (Decimal(output_tokens) / Decimal(1_000_000)) * rates["output_per_m1"]
        total = (cost_input + cost_output).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        return total

    @staticmethod
    async def log_ai_usage(
        user_id: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        latency_ms: float,
        provider: str,
        context: str = "chat",
    ):
        """
        Log AI usage. If DB is unavailable, write a warning — don't silently drop without notice.
        Consider batching/in-memory queueing for high volume.
        """
        if not db.pool:
            logger.warning("AI usage not logged (db unavailable)", extra={"user_id": user_id})
            return

        # guard inputs
        input_tokens = max(0, int(input_tokens))
        output_tokens = max(0, int(output_tokens))
        latency_ms = float(latency_ms)

        est_cost = MetricsEngine._calculate_cost(model, input_tokens, output_tokens)

        try:
            query = """
                INSERT INTO public.ai_usage_logs
                (user_id, model, provider, input_tokens, output_tokens, est_cost, latency_ms, context, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW())
            """
            # Note: asyncpg accepts Decimal for numeric types if column is numeric/decimal
            await db.execute(
                query,
                user_id,
                model,
                provider,
                input_tokens,
                output_tokens,
                float(est_cost),  # or Decimal depending on driver/column type
                latency_ms,
                context,
            )
        except Exception:
            logger.exception("Failed to write ai_usage_logs", extra={"user_id": user_id, "model": model})

    @staticmethod
    async def get_ai_spend_analytics(user_id: str, days: int = 30) -> Dict[str, Any]:
        """
        Aggregate costs for the given window. Uses parameterized numeric interval calculation.
        """
        if not db.pool:
            logger.warning("get_ai_spend_analytics called but db unavailable", extra={"user_id": user_id})
            return {}

        if days <= 0:
            days = 30

        try:
            # Use numeric days * INTERVAL '1 day' which is safe with parameterized $2
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
    async def log_telemetry(
        user_id: str,
        event_type: str,
        category: str = "INFO",
        details: Dict[str, Any] = None,
        path: str = None,
    ):
        if not db.pool:
            logger.warning("telemetry not logged (db unavailable)", extra={"user_id": user_id, "event": event_type})
            return

        try:
            query = """
                INSERT INTO public.user_events (user_id, event_type, category, details, path, created_at)
                VALUES ($1, $2, $3, $4::jsonb, $5, NOW())
            """
            update_active = "UPDATE public.user_profiles SET last_active_at = NOW() WHERE id = $1"

            async with db.pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(query, user_id, event_type, category, details or {}, path)
                    await conn.execute(update_active, user_id)

        except Exception:
            logger.exception("Telemetry log failed", extra={"user_id": user_id, "event": event_type})

    @staticmethod
    async def get_user_insights(user_id: str) -> Dict[str, Any]:
        if not db.pool:
            logger.warning("get_user_insights db unavailable", extra={"user_id": user_id})
            return {}

        insights = {"engagement": {"status": "UNKNOWN"}, "behavior": {"persona": "NEWBIE"}, "health_score": 100}
        try:
            profile = await db.fetch_one("SELECT last_active_at FROM public.user_profiles WHERE id = $1", user_id)
            if profile and profile["last_active_at"]:
                last_active = profile["last_active_at"]
                now = datetime.now(timezone.utc)
                # ensure both tz-aware
                days_inactive = (now - last_active).days
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
