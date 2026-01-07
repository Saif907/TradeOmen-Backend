# backend/app/services/quota_manager.py

import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional

from fastapi import HTTPException, status
from app.core.database import db
from app.core.config import settings

logger = logging.getLogger(__name__)

# Safety guard to prevent context destruction
MAX_TOKENS_PER_REQUEST = 10000

class QuotaManager:
    """
    Enforces SaaS limits using Atomic Operations, Immediate DB Updates, and Lazy Resets.
    """

    @staticmethod
    def _get_safe_plan(user_profile: Dict[str, Any]) -> str:
        """
        Hardening: Handles missing/null plan keys gracefully.
        """
        return (user_profile.get("plan_tier") or settings.DEFAULT_PLAN).upper()

    @staticmethod
    def get_limits(plan_tier: str) -> Dict[str, Any]:
        return settings.get_plan_limits(plan_tier)

    @staticmethod
    async def reset_daily_chat_if_needed(user_id: str, user_profile: Dict[str, Any]):
        """
        ✅ FIX 1: Dedicated Daily Reset Helper.
        Called before any chat logic to ensure we start from 0 on a new day.
        """
        if settings.IS_TEST: return

        last_reset = user_profile.get("last_chat_reset_at")
        now = datetime.now(timezone.utc)

        # Reset if never reset OR if last reset was yesterday (or earlier)
        if not last_reset or (now.date() > last_reset.date()):
            logger.info(f"🔄 Performing Daily Chat Reset for {user_id}")
            await db.execute("""
                UPDATE public.user_profiles
                SET daily_chat_count = 0,
                    last_chat_reset_at = NOW()
                WHERE id = $1
            """, user_id)
            # Update local state so downstream logic knows it's fresh
            user_profile["daily_chat_count"] = 0
            user_profile["last_chat_reset_at"] = now

    @staticmethod
    def check_feature_access(user_profile: Dict[str, Any], feature_flag: str):
        if settings.IS_TEST: return
        
        plan = QuotaManager._get_safe_plan(user_profile)
        if plan == "PREMIUM": return 

        limits = QuotaManager.get_limits(plan)
        
        if not limits.get(feature_flag, False):
            names = {
                "allow_web_search": "Real-time Web Search",
                "allow_broker_sync": "Automated Broker Sync",
                "allow_export_csv": "Data Export (CSV)",
                "allow_deep_research": "Deep Research Agents",
                "allow_tags": "Trade Tagging",
                "allow_screenshots": "Screenshot Uploads"
            }
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"🔒 {names.get(feature_flag, feature_flag)} is locked on the {plan} plan."
            )

    @staticmethod
    async def check_usage_limit(
        user_id: str,
        user_profile: Dict[str, Any], 
        limit_key: str, 
        current_usage_key: str, 
        reset_key: Optional[str] = None,
        reset_frequency: str = "daily"
    ):
        """
        Generic checker for boolean/counter limits (CSV, etc.)
        """
        if settings.IS_TEST: return

        plan = QuotaManager._get_safe_plan(user_profile)
        if plan == "PREMIUM": return

        limits = QuotaManager.get_limits(plan)
        limit_val = limits.get(limit_key, 0)
        
        if limit_val is None: return # Unlimited

        current_val = user_profile.get(current_usage_key, 0)
        
        # Check for resets
        if reset_key:
            last_reset = user_profile.get(reset_key)
            should_reset = False
            
            if not last_reset:
                should_reset = True
            else:
                now = datetime.now(timezone.utc)
                if reset_frequency == "daily":
                    if now.date() > last_reset.date(): should_reset = True
                elif reset_frequency == "monthly":
                    if last_reset.month != now.month or last_reset.year != now.year: should_reset = True

            if should_reset:
                logger.info(f"🔄 Resetting {current_usage_key} for {user_id}")
                await db.execute(f"""
                    UPDATE public.user_profiles 
                    SET {current_usage_key} = 0, {reset_key} = NOW() 
                    WHERE id = $1
                """, user_id)
                current_val = 0

        if current_val >= limit_val:
             raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=f"Quota exceeded for {limit_key.replace('_', ' ')} ({limit_val}). Upgrade for more."
            )

    @staticmethod
    async def reserve_ai_tokens(user_id: str, user_profile: Dict[str, Any], estimated_tokens: int):
        """
        ✅ FIX 2: Atomic AI Reservation + Lazy Monthly Reset.
        """
        if settings.IS_TEST: return
        
        # 🛡️ Advanced Safeguard: Prevent massive context attacks
        if estimated_tokens > MAX_TOKENS_PER_REQUEST:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Request too large ({estimated_tokens} tokens). Max allowed: {MAX_TOKENS_PER_REQUEST}"
            )

        plan = QuotaManager._get_safe_plan(user_profile)
        if plan == "PREMIUM": return

        limits = QuotaManager.get_limits(plan)
        token_limit = limits.get("monthly_ai_tokens_limit", 0)
        
        if token_limit is None: return

        # 🔄 Lazy Monthly Reset Check
        last_reset = user_profile.get("quota_reset_at")
        now = datetime.now(timezone.utc)
        
        if not last_reset or last_reset.month != now.month or last_reset.year != now.year:
            logger.info(f"📅 Performing Monthly Token Reset for {user_id}")
            await db.execute("""
                UPDATE public.user_profiles
                SET monthly_ai_tokens_used = 0,
                    quota_reset_at = NOW()
                WHERE id = $1
            """, user_id)
            # Local update isn't strictly necessary for the SQL query below, 
            # but good for consistency if we add python logic later.

        # ⚛️ Atomic Reservation
        # We try to add tokens. The WHERE clause ensures we don't exceed the limit.
        query = """
            UPDATE public.user_profiles
            SET monthly_ai_tokens_used = monthly_ai_tokens_used + $2
            WHERE id = $1
            AND (monthly_ai_tokens_used + $2) <= $3
            RETURNING monthly_ai_tokens_used
        """
        val = await db.fetch_val(query, user_id, estimated_tokens, token_limit)
        
        if val is None:
            # If fetch_val returns None, the WHERE clause failed -> Limit Exceeded
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=f"Insufficient AI tokens. Limit: {token_limit}. Upgrade to continue."
            )

    @staticmethod
    async def check_trade_storage_limit(user_id: str, user_profile: Dict[str, Any]):
        if settings.IS_TEST: return
        
        plan = QuotaManager._get_safe_plan(user_profile)
        if plan == "PREMIUM": return

        limits = QuotaManager.get_limits(plan)
        max_trades = limits.get("max_trades_per_month", 30)
        
        if max_trades is None: return
        if not db.pool: return

        # Count trades ONLY from the current month
        query = """
            SELECT count(*) as count 
            FROM trades 
            WHERE user_id = $1 
            AND created_at >= date_trunc('month', NOW())
        """
        res = await db.fetch_one(query, user_id)
        count = res["count"] if res else 0
        
        if count >= max_trades:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=f"Monthly trade limit reached ({max_trades}). Upgrade to PRO for unlimited journaling."
            )

    @staticmethod
    async def increment_usage(user_id: str, metric_type: str, extra_data: Dict = None):
        """
        Increments counters that were NOT atomically handled (like CSVs or msg count).
        """
        if not db.pool: return
        try:
            if metric_type == "chat_message":
                # Token reservation happened atomically already. 
                # We just increment the message counter for stats/spam limits.
                await db.execute("""
                    UPDATE public.user_profiles
                    SET daily_chat_count = daily_chat_count + 1
                    WHERE id = $1
                """, user_id)

            elif metric_type == "csv_import":
                # CSV limit was checked via check_usage_limit, now we commit the increment
                await db.execute("""
                    UPDATE public.user_profiles 
                    SET monthly_import_count = monthly_import_count + 1 
                    WHERE id = $1
                """, user_id)
                
        except Exception as e:
            logger.error(f"Failed to increment stats for {user_id}: {e}")

    @staticmethod
    async def get_user_usage_report(user_id: str) -> Dict[str, Any]:
        """
        Fetches current usage metrics for the UI dashboard.
        """
        if not db.pool: return {}
        
        query = """
            SELECT plan_tier, daily_chat_count, monthly_import_count, 
                   monthly_ai_tokens_used, quota_reset_at 
            FROM public.user_profiles WHERE id = $1
        """
        row = await db.fetch_one(query, user_id)
        
        if not row: return {}
        
        data = dict(row)
        plan = QuotaManager._get_safe_plan(data)
        limits = QuotaManager.get_limits(plan)
        
        # Trade count requires separate query
        trade_res = await db.fetch_one("""
            SELECT count(*) as count FROM trades 
            WHERE user_id = $1 AND created_at >= date_trunc('month', NOW())
        """, user_id)
        trade_count = trade_res["count"] if trade_res else 0
        
        return {
            "plan": plan,
            "chat": {
                "used": data["daily_chat_count"],
                "limit": limits.get("daily_chat_msgs", 0)
            },
            "imports": {
                "used": data["monthly_import_count"],
                "limit": limits.get("monthly_csv_imports", 0)
            },
            "trades": {
                "used": trade_count,
                "limit": limits.get("max_trades_per_month", 0)
            },
            "ai_cost_tokens": data["monthly_ai_tokens_used"]
        }