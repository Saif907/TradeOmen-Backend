from typing import Dict, Any
from fastapi import Depends, HTTPException, status

from app.auth.dependency import get_current_user
from app.core.database import db
from app.services.quota_manager import QuotaManager

async def check_ai_quota(
    current_user: Dict[str, Any] = Depends(get_current_user)
) -> Dict[str, Any]:
    """
    1. Fetches the latest user profile (live counters).
    2. Enforces daily chat limits via QuotaManager.
    3. Returns the full profile so the Router doesn't need to fetch it again.
    """
    user_id = current_user["sub"]
    
    # Fetch live data from DB because JWT 'plan' might be outdated 
    # and we definitely need the up-to-the-second 'daily_chat_count'
    if db.pool:
        query = """
            SELECT id, plan_tier, daily_chat_count, last_chat_reset_at 
            FROM public.user_profiles 
            WHERE id = $1
        """
        row = await db.fetch_one(query, user_id)
        # Convert record to dict, or fallback to JWT if DB fails (rare)
        user_profile = dict(row) if row else current_user
    else:
        user_profile = current_user

    # Delegate the logic to the Manager
    # This raises HTTPException(402) if limit is exceeded
    QuotaManager.check_usage_limit(
        user_profile,
        limit_key="daily_chat_msgs",
        current_usage_key="daily_chat_count",
        reset_key="last_chat_reset_at"
    )
    
    return user_profile


def check_broker_sync_access(
    current_user: Dict[str, Any] = Depends(get_current_user)
) -> bool:
    """
    Blocks request if user plan does not allow broker syncing.
    Uses QuotaManager to check the boolean flag.
    """
    QuotaManager.check_feature_access(current_user, "allow_broker_sync")
    return True


def check_web_search_access(
    current_user: Dict[str, Any] = Depends(get_current_user)
) -> bool:
    """
    Blocks request if user plan does not allow web search.
    """
    QuotaManager.check_feature_access(current_user, "allow_web_search")
    return True