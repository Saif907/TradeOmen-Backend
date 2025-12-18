# backend/app/services/quota_manager.py
import logging
from datetime import datetime
from typing import Dict, Any, Optional

from fastapi import HTTPException, status
from app.core.database import db

logger = logging.getLogger(__name__)

class PlanLimits:
    FREE = {
        "daily_chat_msgs": 10,
        "monthly_csv_imports": 1,
        "allow_web_search": False,
        "allow_broker_sync": False,
        "allow_csv_export": False,
        "max_strategies": 1,
        "max_total_trades": 100,
    }
    PRO = {
        "daily_chat_msgs": 500,
        "monthly_csv_imports": 100,
        "allow_web_search": True,
        "allow_broker_sync": True,
        "allow_csv_export": True,
        "max_strategies": 50,
        "max_total_trades": 100_000,
    }
    FOUNDER = {
        "daily_chat_msgs": 1_000_000,
        "monthly_csv_imports": 1_000_000,
        "allow_web_search": True,
        "allow_broker_sync": True,
        "allow_csv_export": True,
        "max_strategies": 1_000,
        "max_total_trades": 1_000_000,
    }

class QuotaManager:
    """
    Central service for enforcing Freemium limits and tracking usage metrics.
    """

    @staticmethod
    def get_limits(plan_tier: str) -> Dict[str, Any]:
        plan_tier = (plan_tier or "FREE").upper()
        if plan_tier == "FOUNDER":
            return PlanLimits.FOUNDER
        elif plan_tier == "PRO":
            return PlanLimits.PRO
        else:
            return PlanLimits.FREE

    @staticmethod
    def check_feature_access(user_profile: Dict[str, Any], feature_flag: str):
        """
        Verifies if the user's plan allows a specific boolean feature.
        Raises 403 if denied.
        """
        plan = user_profile.get("plan_tier", "FREE")
        
        # ✅ Immediate Bypass for Founder
        if plan == "FOUNDER":
            return

        limits = QuotaManager.get_limits(plan)
        
        if not limits.get(feature_flag, False):
            # Map technical flags to user-friendly names for the error message
            names = {
                "allow_web_search": "Real-time Market Search",
                "allow_broker_sync": "Automated Broker Sync",
                "allow_csv_export": "Data Export (CSV)"
            }
            name = names.get(feature_flag, feature_flag)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"{name} is a PRO feature. Please upgrade to access."
            )

    @staticmethod
    def check_usage_limit(
        user_profile: Dict[str, Any], 
        limit_key: str, 
        current_usage_key: str, 
        reset_key: Optional[str] = None
    ):
        """
        Verifies if a numeric counter has exceeded the plan limit.
        """
        plan = user_profile.get("plan_tier", "FREE")
        
        # ✅ Immediate Bypass for Founder
        if plan == "FOUNDER":
            return

        limits = QuotaManager.get_limits(plan)
        limit_val = limits.get(limit_key, 0)
        
        current_val = user_profile.get(current_usage_key, 0)
        
        # Check if daily counter needs a reset (Lazy Logic)
        if reset_key:
            last_reset = user_profile.get(reset_key)
            if not last_reset or (datetime.now(last_reset.tzinfo) - last_reset).days >= 1:
                current_val = 0
                user_profile["_needs_daily_reset"] = True

        if current_val >= limit_val:
             raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=f"Quota exceeded for {limit_key.replace('_', ' ')} ({limit_val}). Upgrade for more."
            )

    @staticmethod
    async def check_trade_storage_limit(user_id: str, user_profile: Dict[str, Any]):
        """
        Checks total trade count against the limit.
        """
        plan = user_profile.get("plan_tier", "FREE")
        if plan == "FOUNDER": return

        limits = QuotaManager.get_limits(plan)
        max_trades = limits.get("max_total_trades", 100)

        if not db.pool: return

        # FIX: Use fetch_one instead of fetch_val if fetch_val is missing
        # We name the count column explicitly to retrieve it easily
        res = await db.fetch_one("SELECT count(*) as count FROM trades WHERE user_id = $1", user_id)
        count = res["count"] if res else 0
        
        if count >= max_trades:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=f"Trade storage limit reached ({max_trades} trades). Upgrade to PRO for unlimited journaling."
            )

    @staticmethod
    async def increment_usage(user_id: str, metric_type: str, extra_data: Dict = None):
        """
        Updates DB counters.
        """
        if not db.pool: return

        try:
            if metric_type == "chat_message":
                needs_reset = extra_data.get("needs_reset", False) if extra_data else False
                tokens = extra_data.get("tokens", 0) if extra_data else 0
                
                if needs_reset:
                    query = """
                        UPDATE public.user_profiles
                        SET daily_chat_count = 1,
                            last_chat_reset_at = NOW(),
                            monthly_ai_tokens_used = monthly_ai_tokens_used + $2
                        WHERE id = $1
                    """
                    await db.execute(query, user_id, tokens)
                else:
                    query = """
                        UPDATE public.user_profiles
                        SET daily_chat_count = daily_chat_count + 1,
                            monthly_ai_tokens_used = monthly_ai_tokens_used + $2
                        WHERE id = $1
                    """
                    await db.execute(query, user_id, tokens)

            elif metric_type == "csv_import":
                query = "UPDATE public.user_profiles SET monthly_import_count = monthly_import_count + 1 WHERE id = $1"
                await db.execute(query, user_id)
                
        except Exception as e:
            logger.error(f"Failed to increment stats for {user_id}: {e}")

    @staticmethod
    async def get_user_usage_report(user_id: str) -> Dict[str, Any]:
        """
        Fetches current usage metrics.
        """
        if not db.pool: return {}
        
        profile_query = """
            SELECT plan_tier, daily_chat_count, monthly_import_count, 
                   monthly_ai_tokens_used, quota_reset_at 
            FROM public.user_profiles WHERE id = $1
        """
        profile_row = await db.fetch_one(profile_query, user_id)
        
        # FIX: Use fetch_one instead of fetch_val
        trade_res = await db.fetch_one("SELECT count(*) as count FROM trades WHERE user_id = $1", user_id)
        trade_count = trade_res["count"] if trade_res else 0
        
        if not profile_row: return {}
        
        data = dict(profile_row)
        limits = QuotaManager.get_limits(data["plan_tier"])
        
        return {
            "plan": data["plan_tier"],
            "chat": {
                "used": data["daily_chat_count"],
                "limit": limits["daily_chat_msgs"]
            },
            "imports": {
                "used": data["monthly_import_count"],
                "limit": limits["monthly_csv_imports"]
            },
            "trades": {
                "used": trade_count,
                "limit": limits["max_total_trades"]
            },
            "ai_cost_tokens": data["monthly_ai_tokens_used"]
        }