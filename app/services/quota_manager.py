# backend/app/services/quota_manager.py

import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional

from app.core.database import db
from app.core.config import settings

logger = logging.getLogger("tradeomen.quota")

MAX_TOKENS_PER_REQUEST = 10_000


# ------------------------------------------------------------------
# Domain Errors (DO NOT USE HTTPException HERE)
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
        return (user_profile.get("plan_tier") or settings.DEFAULT_PLAN).upper()

    @staticmethod
    def limits(plan: str) -> Dict[str, Any]:
        return settings.get_plan_limits(plan)

    # --------------------------------------------------------------
    # Feature Flags
    # --------------------------------------------------------------

    @staticmethod
    def require_feature(user_profile: Dict[str, Any], flag: str) -> None:
        plan = QuotaManager._plan(user_profile)
        if plan == "PREMIUM":
            return

        if not QuotaManager.limits(plan).get(flag, False):
            raise FeatureLockedError(flag)

    # --------------------------------------------------------------
    # AI TOKEN RESERVATION (SOURCE OF TRUTH)
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
        if plan == "PREMIUM":
            return

        limits = QuotaManager.limits(plan)
        token_limit = limits.get("monthly_ai_tokens_limit")

        if token_limit is None:
            return

        now = datetime.now(timezone.utc)

        try:
            async with db.transaction() as conn:
                # Lazy monthly reset
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

                # Atomic reservation
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
    # DAILY MESSAGE COUNT (ATOMIC)
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

    # --------------------------------------------------------------
    # CSV IMPORT
    # --------------------------------------------------------------

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

    # --------------------------------------------------------------
    # READ-ONLY USAGE (FOR UI)
    # --------------------------------------------------------------

    @staticmethod
    async def get_usage(user_id: str) -> Dict[str, Any]:
        try:
            row = await db.fetch_one("""
                SELECT plan_tier,
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

        plan = QuotaManager._plan(dict(row))
        limits = QuotaManager.limits(plan)

        return {
            "plan": plan,
            "chat": {
                "used": row["daily_chat_count"],
                "limit": limits.get("daily_chat_msgs"),
            },
            "imports": {
                "used": row["monthly_import_count"],
                "limit": limits.get("monthly_csv_imports"),
            },
            "ai_tokens": {
                "used": row["monthly_ai_tokens_used"],
                "limit": limits.get("monthly_ai_tokens_limit"),
            },
        }
