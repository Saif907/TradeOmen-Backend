# backend/app/services/quota_manager.py

import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional

from fastapi import HTTPException, status
from app.core.database import db
from app.core.config import settings

# ✅ NEW IMPORT: Required for Write-Through Caching
from app.auth.dependency import update_user_cache

logger = logging.getLogger("tradeomen.quota")

MAX_TOKENS_PER_REQUEST = 10_000


# ------------------------------------------------------------------
# Domain Errors
# ------------------------------------------------------------------

class QuotaError(Exception):
    pass

class QuotaExceededError(QuotaError):
    pass

class FeatureLockedError(QuotaError):
    pass

class QuotaServiceUnavailable(QuotaError):
    pass


# ------------------------------------------------------------------
# Quota Manager
# ------------------------------------------------------------------

class QuotaManager:

    # --------------------------------------------------------------
    # 1. Plan & Limit Resolution
    # --------------------------------------------------------------
    @staticmethod
    def _plan(user_profile: Dict[str, Any]) -> str:
        """
        Resolves the effective plan ID from the user context.
        Handles aliases (e.g. LIFETIME -> PREMIUM).
        """
        raw_plan = (
            user_profile.get("plan_tier") or 
            user_profile.get("active_plan_id") or 
            user_profile.get("plan_id") or
            user_profile.get("plan") or 
            settings.DEFAULT_PLAN
        ).upper()
        
        # Normalize Legacy/Alias plans
        if raw_plan in ("FOUNDER", "LIFETIME", "LIFETIME_PRO"):
            return "PREMIUM"
            
        return raw_plan

    @staticmethod
    def limits(plan: str) -> Dict[str, Any]:
        return settings.get_plan_limits(plan)

    # --------------------------------------------------------------
    # 2. Universal Permission Check (Features)
    # --------------------------------------------------------------
    @staticmethod
    def require_feature(user: Dict[str, Any], flag: str) -> None:
        """
        Zero-DB check for feature flags (e.g. 'allow_screenshots').
        """
        plan = QuotaManager._plan(user)
        limits = QuotaManager.limits(plan)
        
        if not limits.get(flag, False):
             raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Plan upgrade required for feature: {flag}"
            )

    # --------------------------------------------------------------
    # 3. Chat & AI Limits (Optimized)
    # --------------------------------------------------------------
    @staticmethod
    def validate_chat_access(user: Dict[str, Any]) -> None:
        """
        Checks if the user can send a chat message.
        - Premium: Returns immediately (0 DB calls).
        - Free: Checks cached counters from dependency.py (0 DB calls).
        """
        plan = QuotaManager._plan(user)
        limits = QuotaManager.limits(plan)
        
        # ✅ FAST PATH: If limit is None (Unlimited), return immediately.
        limit_count = limits.get("daily_chat_msgs")
        if limit_count is None:
            return

        # LOGIC: Lazy Reset (In-Memory)
        # We rely on the data fetched in dependency.py (cached for 180s)
        last_reset = user.get("last_chat_reset_at")
        current_usage = user.get("daily_chat_count", 0)
        
        now = datetime.now(timezone.utc)
        
        # If reset was yesterday (or never), effective usage is 0
        if not last_reset:
             current_usage = 0
        else:
             # Handle string vs datetime object from DB driver
             if isinstance(last_reset, str):
                 try:
                     last_reset = datetime.fromisoformat(last_reset)
                 except ValueError:
                     last_reset = now # Fallback
                     
             # If cached reset time is from a previous day, usage is effectively 0
             if last_reset.date() < now.date():
                 current_usage = 0

        if current_usage >= limit_count:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Daily chat limit ({limit_count}) reached. Upgrade for unlimited."
            )

    @staticmethod
    async def reserve_ai_tokens(
        user_id: str,
        user_profile: Dict[str, Any],
        estimated_tokens: int,
    ) -> None:
        """
        Reserves tokens for AI usage.
        Premium users skip the DB transaction entirely.
        Updates Cache on success.
        """
        if estimated_tokens > MAX_TOKENS_PER_REQUEST:
            raise QuotaExceededError("Request too large")

        plan = QuotaManager._plan(user_profile)
        limits = QuotaManager.limits(plan)
        
        # ✅ FAST PATH: Unlimited plans skip DB reservation
        token_limit = limits.get("monthly_ai_tokens_limit")
        if token_limit is None:
            return

        now = datetime.now(timezone.utc)

        try:
            async with db.transaction() as conn:
                # 1. Lazy Reset if needed
                await conn.execute("""
                    UPDATE public.user_profiles
                    SET monthly_ai_tokens_used = 0,
                        quota_reset_at = $2
                    WHERE id = $1
                      AND (
                        quota_reset_at IS NULL OR
                        quota_reset_at < date_trunc('month', $2::timestamptz)
                      )
                """, user_id, now)

                # 2. Atomic Increment & Check (RETURNING new value)
                # ✅ FIX: Use fetch_val to get the new usage for cache update
                new_usage = await conn.fetch_val("""
                    UPDATE public.user_profiles
                    SET monthly_ai_tokens_used = monthly_ai_tokens_used + $2
                    WHERE id = $1
                      AND monthly_ai_tokens_used + $2 <= $3
                    RETURNING monthly_ai_tokens_used
                """, user_id, estimated_tokens, token_limit)

                if new_usage is None:
                    raise QuotaExceededError("Monthly AI token limit exceeded")
                
                # ✅ FIX: Update Cache Immediately (Write-Through)
                update_user_cache(user_id, {"monthly_ai_tokens_used": new_usage})

        except QuotaError:
            raise
        except Exception:
            logger.exception("Token reservation failed")
            raise QuotaServiceUnavailable()

    # --------------------------------------------------------------
    # 4. Database Bound Checks (Trades/Strategies)
    # --------------------------------------------------------------
    @staticmethod
    async def check_trade_limit(user: Dict[str, Any]) -> None:
        """
        Checks if user has reached monthly trade limit.
        Premium users skip the count query.
        """
        plan = QuotaManager._plan(user)
        limits = QuotaManager.limits(plan)
        max_trades = limits.get("max_trades_per_month")
        
        # ✅ FAST PATH: Premium users skip the COUNT(*) query
        if max_trades is None:
            return

        # Only run Count Query for Free/Pro users
        user_id = user["user_id"]
        count = await db.fetch_val("""
            SELECT COUNT(*) FROM trades 
            WHERE user_id = $1 
            AND created_at >= date_trunc('month', NOW())
        """, user_id)
        
        if count >= max_trades:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS, 
                detail=f"Monthly trade limit ({max_trades}) reached."
            )

    @staticmethod
    async def check_strategy_limit(user: Dict[str, Any]) -> None:
        plan = QuotaManager._plan(user)
        limits = QuotaManager.limits(plan)
        max_strat = limits.get("max_strategies")
        
        if max_strat is None: 
            return

        count = await db.fetch_val("SELECT COUNT(*) FROM strategies WHERE user_id = $1", user["user_id"])
        if count >= max_strat:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS, 
                detail=f"Strategy limit ({max_strat}) reached."
            )

    # --------------------------------------------------------------
    # 5. Background Updates (Fire & Forget with Cache Sync)
    # --------------------------------------------------------------
    @staticmethod
    async def increment_daily_chat(user_id: str) -> None:
        """
        Increments the chat counter in the background. 
        Updates DB AND Cache.
        """
        try:
            # ✅ FIX: Use fetch_val with RETURNING to get new count for cache
            new_count = await db.fetch_val("""
                UPDATE public.user_profiles
                SET daily_chat_count =
                    CASE
                        WHEN last_chat_reset_at IS NULL
                          OR last_chat_reset_at < date_trunc('day', NOW())
                        THEN 1
                        ELSE daily_chat_count + 1
                    END,
                    last_chat_reset_at = NOW()
                WHERE id = $1
                RETURNING daily_chat_count
            """, user_id)
            
            # ✅ FIX: Update Cache Immediately (Write-Through)
            if new_count is not None:
                update_user_cache(user_id, {
                    "daily_chat_count": new_count,
                    "last_chat_reset_at": datetime.now() # Approximate is fine for cache
                })
                
        except Exception:
            logger.exception("Failed to increment chat counter")
            pass

    @staticmethod
    async def increment_csv_import(user_id: str) -> None:
        try:
            # ✅ FIX: Use fetch_val with RETURNING
            new_count = await db.fetch_val("""
                UPDATE public.user_profiles
                SET monthly_import_count =
                    CASE
                        WHEN quota_reset_at IS NULL
                          OR quota_reset_at < date_trunc('month', NOW())
                        THEN 1
                        ELSE monthly_import_count + 1
                    END,
                    quota_reset_at = NOW()
                WHERE id = $1
                RETURNING monthly_import_count
            """, user_id)
            
            # ✅ FIX: Update Cache Immediately
            if new_count is not None:
                update_user_cache(user_id, {"monthly_import_count": new_count})

        except Exception:
            logger.exception("CSV quota increment failed")
            pass

    @staticmethod
    async def get_usage(user_id: str) -> Dict[str, Any]:
        """
        API Endpoint Helper: Returns current usage stats.
        """
        try:
            row = await db.fetch_one("""
                SELECT plan_tier,
                       active_plan_id,
                       daily_chat_count,
                       monthly_import_count,
                       monthly_ai_tokens_used
                FROM public.user_profiles
                WHERE id = $1
            """, user_id)
        except Exception:
            raise QuotaServiceUnavailable()

        if not row:
            return {}

        row_dict = dict(row)
        plan = QuotaManager._plan(row_dict)
        limits = QuotaManager.limits(plan)

        return {
            "plan": plan,
            "chat": {
                "used": row_dict["daily_chat_count"],
                "limit": limits.get("daily_chat_msgs"),
            },
            "imports": {
                "used": row_dict["monthly_import_count"],
                "limit": limits.get("monthly_csv_imports"),
            },
            "ai_tokens": {
                "used": row_dict["monthly_ai_tokens_used"],
                "limit": limits.get("monthly_ai_tokens_limit"),
            },
        }