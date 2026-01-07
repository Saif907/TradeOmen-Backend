# backend/app/auth/permissions.py

from typing import Dict, Any
from fastapi import Depends, HTTPException, status

from app.auth.dependency import get_current_user
from app.core.database import db
from app.services.quota_manager import QuotaManager

async def _get_fresh_profile(user_id: str, current_user: Dict[str, Any]) -> Dict[str, Any]:
    """
    Helper: Fetches the latest user profile stats from DB.
    Falls back to the JWT content if DB is unreachable.
    """
    if db.pool:
        # Fetch all fields needed for quota checks (counters + dates)
        query = """
            SELECT id, plan_tier, 
                   daily_chat_count, last_chat_reset_at,
                   monthly_ai_tokens_used, monthly_import_count, quota_reset_at
            FROM public.user_profiles 
            WHERE id = $1
        """
        row = await db.fetch_one(query, user_id)
        return dict(row) if row else current_user
    return current_user

async def check_ai_quota(
    current_user: Dict[str, Any] = Depends(get_current_user)
) -> Dict[str, Any]:
    """
    Validates User Plan & Quotas before allowing AI access.
    """
    user_id = current_user["sub"]
    user_profile = await _get_fresh_profile(user_id, current_user)

    # 1. Ensure Daily Chat Counter is fresh (Reset if new day)
    # We do this here because the user might just be reading chat history, 
    # and we want the UI to show "0/5" used correctly.
    await QuotaManager.reset_daily_chat_if_needed(user_id, user_profile)

    # 2. Check Daily Messages Limit
    await QuotaManager.check_usage_limit(
        user_id=user_id,
        user_profile=user_profile,
        limit_key="daily_chat_msgs",
        current_usage_key="daily_chat_count",
        reset_key="last_chat_reset_at",
        reset_frequency="daily"
    )

    # 3. Check Monthly Token Limit (Fail Fast Only)
    # ✅ FIX: Removed reset logic here. 
    # The actual atomic monthly reset happens in QuotaManager.reserve_ai_tokens()
    # This just prevents wasting resources if the user is clearly already over the limit.
    await QuotaManager.check_usage_limit(
        user_id=user_id,
        user_profile=user_profile,
        limit_key="monthly_ai_tokens_limit",
        current_usage_key="monthly_ai_tokens_used"
        # reset_key & reset_frequency REMOVED to avoid double-reset
    )
    
    return user_profile

async def check_import_quota(
    current_user: Dict[str, Any] = Depends(get_current_user)
) -> Dict[str, Any]:
    """
    Dependency: Enforces CSV Import Limits.
    """
    user_id = current_user["sub"]
    user_profile = await _get_fresh_profile(user_id, current_user)

    # Enforce Monthly Import Limit (Keeps reset logic as CSVs don't use reserve_ai_tokens)
    await QuotaManager.check_usage_limit(
        user_id=user_id,
        user_profile=user_profile,
        limit_key="monthly_csv_imports",
        current_usage_key="monthly_import_count",
        reset_key="quota_reset_at",
        reset_frequency="monthly"
    )
    
    return user_profile

async def check_broker_sync_access(
    current_user: Dict[str, Any] = Depends(get_current_user)
) -> bool:
    """
    Blocks request if user plan does not allow broker syncing.
    """
    user_id = current_user["sub"]
    user_profile = await _get_fresh_profile(user_id, current_user)
    
    QuotaManager.check_feature_access(user_profile, "allow_broker_sync")
    return True

async def check_web_search_access(
    current_user: Dict[str, Any] = Depends(get_current_user)
) -> bool:
    """
    Blocks request if user plan does not allow web search.
    """
    user_id = current_user["sub"]
    user_profile = await _get_fresh_profile(user_id, current_user)
    
    QuotaManager.check_feature_access(user_profile, "allow_web_search")
    return True

async def check_export_access(
    current_user: Dict[str, Any] = Depends(get_current_user)
) -> bool:
    """
    Blocks request if user plan does not allow CSV exporting.
    """
    user_id = current_user["sub"]
    user_profile = await _get_fresh_profile(user_id, current_user)
    
    QuotaManager.check_feature_access(user_profile, "allow_export_csv")
    return True