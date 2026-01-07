# backend/app/services/metrics_engine.py
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
from app.core.database import db

logger = logging.getLogger(__name__)

class MetricsEngine:
    """
    Central engine for Analytics & Telemetry.
    Now includes Financial Estimation (Cost Tracking) for AI usage.
    """

    # --- CONFIGURATION: Cost per 1M Tokens (Approximate) ---
    # Update this as providers change pricing.
    PRICING_TABLE = {
    # === OPENAI ===
    # GPT-4.1 family
    "gpt-4.1":          {"input_per_m1": 2.00,  "output_per_m1": 8.00},
    "gpt-4.1-mini":     {"input_per_m1": 0.40,  "output_per_m1": 1.60},
    "gpt-4.1-nano":     {"input_per_m1": 0.10,  "output_per_m1": 0.40},

    # GPT-4o family
    "gpt-4o":           {"input_per_m1": 2.50,  "output_per_m1": 10.00},
    "gpt-4o-mini":      {"input_per_m1": 0.15,  "output_per_m1": 0.60},
    # GPT-5 (standard) — solid balance of performance & cost
    "gpt-5":             {"input_per_m1": 1.25,  "output_per_m1": 10.00},  # Approx standard pricing :contentReference[oaicite:1]{index=1}

    # GPT-5.1 (improved general-purpose) — similar priced to GPT-5
    "gpt-5.1":           {"input_per_m1": 1.25,  "output_per_m1": 10.00},  # Approx same as GPT-5 :contentReference[oaicite:2]{index=2}

    # GPT-5 nano — very cheap for simple tasks
    "gpt-5-nano":        {"input_per_m1": 0.05,  "output_per_m1": 0.40},   # Cheap summarisation/classification :contentReference[oaicite:3]{index=3}

    # GPT-5.2 family — newer generation
    # Standard GPT-5.2 (base) — enhanced reasoning & larger context
    "gpt-5.2":           {"input_per_m1": 1.75,  "output_per_m1": 14.00},  # Premium base pricing :contentReference[oaicite:4]{index=4}

    # GPT-5.2 Pro — highest quality, expensive
    "gpt-5.2-pro":       {"input_per_m1": 21.00, "output_per_m1": 168.00}, # Very high-end tier :contentReference[oaicite:5]{index=5}

    # === OPENAI GPT-4 FAMILY ===
    "gpt-4o":            {"input_per_m1": 2.50,  "output_per_m1": 10.00},
    "gpt-4o-mini":       {"input_per_m1": 0.15,  "output_per_m1": 0.60},

    # === GOOGLE GEMINI ===
    #lower cost models
    # Gemini 2.0 Flash (affordable general-purpose)
    "gemini-2.0-flash":    {"input_per_m1": 0.10,  "output_per_m1": 0.40},   # balanced price/performance :contentReference[oaicite:1]{index=1}
    # Gemini 2.5 Flash (affordable general-purpose)
    "gemini-2.5-flash": {"input_per_m1": 0.15,  "output_per_m1": 0.60},   # balanced price/performance :contentReference[oaicite:1]{index=1}

    # Gemini 2.5 Pro (higher quality, reasoning)
    "gemini-2.5-pro":   {"input_per_m1": 1.25,  "output_per_m1": 10.00},  # standard API tier :contentReference[oaicite:2]{index=2}

    # Premium/other Gemini tiers (if available)
    "gemini-3-flash":   {"input_per_m1": 0.50,  "output_per_m1": 3.00},   # newer Flash tier seen in pricing summaries :contentReference[oaicite:3]{index=3}
    "gemini-3-pro":     {"input_per_m1": 2.00,  "output_per_m1": 12.00},   # approximate higher tier :contentReference[oaicite:4]{index=4}

    # === ANTHROPIC CLAUDE ===
    # Claude flagship models (higher token price)
    "claude-opus-4.5":    {"input_per_m1": 15.00, "output_per_m1": 75.00},  # highest quality tier
    "claude-sonnet-4.5":  {"input_per_m1": 3.00,  "output_per_m1": 15.00},  # mid tier

    # === PERPLEXITY SONAR FAMILY ===
    # Based on available token pricing data
    "perplexity-sonar-pro":  {"input_per_m1": 3.00,  "output_per_m1": 15.00},  # Sonar Pro pricing (approx) :contentReference[oaicite:5]{index=5}
    "perplexity-sonar-lite": {"input_per_m1": 1.50,  "output_per_m1": 6.00},   # lighter Sonar variant (approx) :contentReference[oaicite:6]{index=6}

    # Fallback/default
    "default":           {"input_per_m1": 1.00,  "output_per_m1": 3.00}
}


    # ---------------------------------------------------------
    # 1. AI & COST TRACKING
    # ---------------------------------------------------------

    @staticmethod
    def _calculate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
        """Helper to calculate estimated cost in USD."""
        rates = MetricsEngine.PRICING_TABLE.get(model, MetricsEngine.PRICING_TABLE["default"])
        
        # Prices are usually per 1 Million tokens
        cost_input = (input_tokens / 1_000_000) * rates["input"]
        cost_output = (output_tokens / 1_000_000) * rates["output"]
        
        return round(cost_input + cost_output, 7) # High precision for micro-transactions

    @staticmethod
    async def log_ai_usage(
        user_id: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        latency_ms: float,
        provider: str,
        context: str = "chat"
    ):
        """
        Logs detailed AI usage for Cost Analysis and Performance Monitoring.
        Recommended: Store this in a dedicated 'ai_usage_logs' table in Postgres.
        """
        if not db.pool: return

        est_cost = MetricsEngine._calculate_cost(model, input_tokens, output_tokens)
        
        try:
            # We use a structured log table for high-volume analytics
            query = """
                INSERT INTO public.ai_usage_logs 
                (user_id, model, provider, input_tokens, output_tokens, est_cost, latency_ms, context, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW())
            """
            await db.execute(
                query, 
                user_id, model, provider, input_tokens, output_tokens, 
                est_cost, latency_ms, context
            )
        except Exception as e:
            # Fallback to standard logging if table doesn't exist yet
            logger.error(f"Failed to log AI usage (DB Error): {e}")

    @staticmethod
    async def get_ai_spend_analytics(user_id: str, days: int = 30) -> Dict[str, Any]:
        """
        Returns AI consumption data for dashboards.
        """
        if not db.pool: return {}
        
        try:
            # Aggregate cost and tokens by Model
            query = """
                SELECT 
                    model, 
                    COUNT(*) as requests,
                    SUM(input_tokens) as total_input,
                    SUM(output_tokens) as total_output,
                    SUM(est_cost) as total_cost,
                    AVG(latency_ms) as avg_latency
                FROM public.ai_usage_logs
                WHERE user_id = $1 AND created_at >= NOW() - INTERVAL '$2 days'
                GROUP BY model
            """
            # Note: Parameter binding for Interval is tricky in AsyncPG, simplified here:
            rows = await db.fetch_all(query.replace("$2", str(days)), user_id)
            
            breakdown = [dict(r) for r in rows]
            total_spend = sum(r["total_cost"] for r in rows)

            return {
                "period": f"last_{days}_days",
                "total_spend_usd": round(total_spend, 4),
                "model_breakdown": breakdown
            }
        except Exception:
            return {"info": "No AI usage data available yet."}


    # ---------------------------------------------------------
    # 2. GENERAL TELEMETRY (Events)
    # ---------------------------------------------------------

    @staticmethod
    async def log_telemetry(
        user_id: str, 
        event_type: str, 
        category: str = "INFO", 
        details: Dict[str, Any] = None,
        path: str = None
    ):
        """
        Records general user events (Signups, Errors, Button Clicks).
        """
        if not db.pool: return
        
        try:
            query = """
                INSERT INTO public.user_events (user_id, event_type, category, details, path)
                VALUES ($1, $2, $3, $4, $5)
            """
            # Update 'last_active_at' for Churn prediction
            update_active = "UPDATE public.user_profiles SET last_active_at = NOW() WHERE id = $1"
            
            async with db.pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(query, user_id, event_type, category, details or {}, path)
                    await conn.execute(update_active, user_id)
                    
        except Exception as e:
            logger.error(f"Telemetry log failed: {e}")

    # ---------------------------------------------------------
    # 3. USER INSIGHTS (Health & Persona)
    # ---------------------------------------------------------

    @staticmethod
    async def get_user_insights(user_id: str) -> Dict[str, Any]:
        """
        Compiles a 'Health Score' and 'Persona' for the user.
        Useful for targeted marketing or support.
        """
        if not db.pool: return {}

        insights = {
            "engagement": {"status": "UNKNOWN"},
            "behavior": {"persona": "NEWBIE"},
            "health_score": 100
        }

        try:
            # A. Engagement (Churn Risk)
            profile = await db.fetch_one("SELECT last_active_at FROM user_profiles WHERE id = $1", user_id)
            if profile and profile["last_active_at"]:
                last_active = profile["last_active_at"]
                days_inactive = (datetime.now(last_active.tzinfo) - last_active).days
                
                status = "ACTIVE"
                if days_inactive > 30: status = "CHURNED"
                elif days_inactive > 14: status = "AT_RISK"
                
                insights["engagement"] = {
                    "days_since_active": days_inactive,
                    "status": status
                }
            
            # B. Persona (Trader Type)
            # Simple heuristic: Manual vs Auto trades
            stats = await db.fetch_one("""
                SELECT 
                    COUNT(*) as total,
                    COUNT(CASE WHEN source_type = 'AUTO_SYNC' THEN 1 END) as synced
                FROM trades WHERE user_id = $1
            """, user_id)
            
            if stats and stats["total"] > 5:
                ratio = stats["synced"] / stats["total"]
                if ratio > 0.8: insights["behavior"]["persona"] = "ALGO_TRADER"
                elif ratio < 0.2: insights["behavior"]["persona"] = "MANUAL_JOURNALER"
                else: insights["behavior"]["persona"] = "HYBRID_TRADER"

        except Exception as e:
            logger.warning(f"Error generating insights: {e}")

        return insights