# backend/app/auth/permissions.py

import logging
from datetime import datetime, timezone
from typing import Dict, Any

from fastapi import Depends, HTTPException, status

from app.auth.dependency import get_current_user
from app.core.database import db
from app.core.config import settings
from app.services.quota_manager import QuotaManager, FeatureLockedError

logger = logging.getLogger("tradeomen.auth.permissions")

# ------------------------------------------------------------------
# Internal Helpers
# ------------------------------------------------------------------

async def _load_fresh_profile(user_id: str) -> Dict[str, Any]:
    """
    Loads the absolute latest usage statistics from the DB.
    """
    query = """
        SELECT 
            id,
            active_plan_id, 
            plan_tier, 
            daily_chat_count, 
            last_chat_reset_at,
            monthly_ai_tokens_used,
            monthly_import_count,
            quota_reset_at
        FROM public.user_profiles 
        WHERE id = $1
    """
    try:
        profile = await db.fetch_one(query, user_id)
        if not profile:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="User profile not found",
            )
        return dict(profile)
    except HTTPException:
        raise
    except Exception:
        logger.exception(f"Failed to load profile for user {user_id}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Permission service unavailable",
        )


def _enforce_feature_flag(user_profile: Dict[str, Any], flag: str):
    """
    Helper to translate domain exceptions into HTTP exceptions.
    """
    try:
        QuotaManager.require_feature(user_profile, flag)
    except FeatureLockedError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Upgrade your plan to access: {flag.replace('_', ' ')}"
        )
    except Exception:
        logger.exception(f"Feature flag check failed for {flag}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not verify feature access"
        )


def _get_effective_chat_usage(profile: Dict[str, Any]) -> int:
    """
    Handles 'Lazy Reset' logic in memory.
    If the reset time was yesterday, effective usage is 0.
    """
    last_reset = profile.get("last_chat_reset_at")
    
    # If never reset, or reset prior to today, usage is effectively 0
    if not last_reset:
        return 0
        
    now = datetime.now(timezone.utc)
    
    # Check if last_reset is on a different day than today (UTC)
    if last_reset.date() < now.date():
        return 0
        
    return profile.get("daily_chat_count", 0)


def _get_effective_token_usage(profile: Dict[str, Any]) -> int:
    """
    Handles 'Lazy Reset' logic for monthly tokens.
    """
    reset_at = profile.get("quota_reset_at")
    
    if not reset_at:
        return 0
        
    now = datetime.now(timezone.utc)
    
    # Check if we are in a new month compared to quota_reset_at
    if (now.year > reset_at.year) or (now.month > reset_at.month):
        return 0
        
    return profile.get("monthly_ai_tokens_used", 0)


# ------------------------------------------------------------------
# AI / Chat Permissions
# ------------------------------------------------------------------

async def validate_ai_usage_limits(
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Read-only check to see if the user has hit hard limits.
    Prevents "Morning Lockout" by calculating effective usage based on dates.
    """
    user_id = current_user["user_id"]
    
    # 1. Single DB Call
    profile = await _load_fresh_profile(user_id)
    
    # 2. Get Plan Limits
    plan = QuotaManager._plan(profile)
    limits = QuotaManager.limits(plan)

    # 3. Check Daily Chat Limit
    # Note: If limit is None, it's unlimited (e.g. some internal tier).
    # If PREMIUM has a number (e.g. 200), this logic will now correctly enforce it.
    chat_limit = limits.get("daily_chat_msgs")
    
    if chat_limit is not None:
        effective_usage = _get_effective_chat_usage(profile)
        if effective_usage >= chat_limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Daily chat limit reached. Please upgrade or wait for reset."
            )

    # 4. Check Monthly Token Hard Cap
    token_limit = limits.get("monthly_ai_tokens_limit")
    if token_limit is not None:
        effective_tokens = _get_effective_token_usage(profile)
        if effective_tokens >= token_limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Monthly AI token limit exceeded."
            )

    return profile


# ------------------------------------------------------------------
# Feature Access Permissions (Router Dependencies)
# ------------------------------------------------------------------

async def check_broker_sync_access(
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> None:
    profile = await _load_fresh_profile(current_user["user_id"])
    _enforce_feature_flag(profile, "allow_broker_sync")


async def check_web_search_access(
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> None:
    profile = await _load_fresh_profile(current_user["user_id"])
    _enforce_feature_flag(profile, "allow_web_search")


async def check_csv_export_access(
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> None:
    profile = await _load_fresh_profile(current_user["user_id"])
    _enforce_feature_flag(profile, "allow_export_csv")


# ------------------------------------------------------------------
# Import Permission
# ------------------------------------------------------------------

async def check_import_quota(
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Checks if the user has remaining CSV import slots.
    """
    user_id = current_user["user_id"]
    
    profile = await _load_fresh_profile(user_id)
    plan = QuotaManager._plan(profile)
    
    # Robust check: rely on limits, not plan name
    limits = QuotaManager.limits(plan)
    limit = limits.get("monthly_csv_imports")
    
    if limit is not None:
        reset_at = profile.get("quota_reset_at")
        now = datetime.now(timezone.utc)
        
        # Calculate effective usage
        effective_usage = profile.get("monthly_import_count", 0)
        
        # Simple monthly reset logic for imports
        if reset_at and (now.year > reset_at.year or now.month > reset_at.month):
            effective_usage = 0
            
        if effective_usage >= limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Monthly CSV import limit reached."
            )
            
    return profile