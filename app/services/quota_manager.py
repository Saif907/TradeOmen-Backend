# backend/app/services/quota_manager.py

import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional

from app.core.database import db
from app.core.config import settings

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

    @staticmethod
    def _plan(user_profile: Dict[str, Any]) -> str:
        # 1. Extract plan from profile
        raw_plan = (
            user_profile.get("plan_tier") or 
            user_profile.get("active_plan_id") or 
            user_profile.get("plan") or 
            settings.DEFAULT_PLAN
        ).upper()
        
        # 2. Normalize Legacy/Alias plans
        if raw_plan in ("FOUNDER", "LIFETIME", "LIFETIME_PRO"):
            return "PREMIUM"
            
        return raw_plan

    @staticmethod
    def limits(plan: str) -> Dict[str, Any]:
        return settings.get_plan_limits(plan)

    # --------------------------------------------------------------
    # Feature Flags (Async for DB Fallback)
    # --------------------------------------------------------------

    @staticmethod
    async def require_feature(user_profile: Dict[str, Any], flag: str) -> None:
        """
        Checks if a feature is allowed based on configuration limits.
        Falls back to DB lookup if the profile dict seems stale.
        """
        # 1. Check using the passed profile object first (Fast)
        plan = QuotaManager._plan(user_profile)
        plan_limits = QuotaManager.limits(plan)
        
        # 2. If explicitly allowed by config, return immediately
        if plan_limits.get(flag, False):
            return

        # 3. If denied/missing, try fetching fresh from DB (Source of Truth)
        #    This handles cases where 'current_user' is a stale JWT payload.
        user_id = user_profile.get("user_id") or user_profile.get("id")
        
        if user_id:
            try:
                # Fetch only the plan fields
                row = await db.fetch_one("""
                    SELECT plan_tier, active_plan_id 
                    FROM public.user_profiles 
                    WHERE id = $1
                """, user_id)
                
                if row:
                    # Re-evaluate with DB data
                    db_plan = QuotaManager._plan(dict(row))
                    db_limits = QuotaManager.limits(db_plan)
                    
                    if db_limits.get(flag, False):
                        return
                    
                    # Update variable for logging
                    plan = db_plan 

            except Exception as e:
                logger.warning(f"Failed to fetch fresh plan for feature check: {e}")

        # 4. If still denied, raise error
        logger.warning(f"Feature locked: {flag} for plan {plan} (User ID: {user_id})")
        raise FeatureLockedError(flag)

    # --------------------------------------------------------------
    # AI TOKEN RESERVATION
    # --------------------------------------------------------------

    @staticmethod
    async def reserve_ai_tokens(
        user_id: str,
        user_profile: Dict[str, Any],
        estimated_tokens: int,
    ) -> None:

        if estimated_tokens > MAX_TOKENS_PER_REQUEST:
            raise QuotaExceededError("Request too large")

        plan = QuotaManager._plan(user_profile)
        limits = QuotaManager.limits(plan)
        
        # Robustness: Rely on config. If limit is None, it is unlimited.
        token_limit = limits.get("monthly_ai_tokens_limit")

        if token_limit is None:
            return

        now = datetime.now(timezone.utc)

        try:
            async with db.transaction() as conn:
                await conn.execute("""
                    UPDATE public.user_profiles
                    SET monthly_ai_tokens_used = 0,
                        quota_reset_at = $2
                    WHERE id = $1
                      AND (
                        quota_reset_at IS NULL OR
                        quota_reset_at < date_trunc('month', $2)
                      )
                """, user_id, now)

                row = await conn.fetchrow("""
                    UPDATE public.user_profiles
                    SET monthly_ai_tokens_used = monthly_ai_tokens_used + $2
                    WHERE id = $1
                      AND monthly_ai_tokens_used + $2 <= $3
                    RETURNING monthly_ai_tokens_used
                """, user_id, estimated_tokens, token_limit)

                if not row:
                    raise QuotaExceededError("AI token limit exceeded")

        except QuotaError:
            raise
        except Exception:
            logger.exception("Token reservation failed")
            raise QuotaServiceUnavailable()

    # --------------------------------------------------------------
    # COUNTERS
    # --------------------------------------------------------------

    @staticmethod
    async def increment_daily_chat(user_id: str) -> None:
        try:
            await db.execute("""
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
            """, user_id)
        except Exception:
            logger.exception("Failed to increment chat counter")
            raise QuotaServiceUnavailable()

    @staticmethod
    async def increment_csv_import(user_id: str) -> None:
        try:
            await db.execute("""
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
            """, user_id)
        except Exception:
            logger.exception("CSV quota increment failed")
            raise QuotaServiceUnavailable()

    @staticmethod
    async def get_usage(user_id: str) -> Dict[str, Any]:
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