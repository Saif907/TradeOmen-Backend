# backend/app/auth/permissions.py

from typing import Dict, Any
from fastapi import Depends, HTTPException, status

from app.auth.dependency import get_current_user
from app.core.database import db
from app.services.quota_manager import QuotaManager


# ------------------------------------------------------------------
# Internal Helper
# ------------------------------------------------------------------

async def _load_user_profile(user_id: str) -> Dict[str, Any]:
    """
    Loads the latest user profile from DB.
    Permissions MUST fail if DB is unavailable.
    """

    query = """
        SELECT
            id,
            plan_tier,
            daily_chat_count,
            last_chat_reset_at,
            monthly_ai_tokens_used,
            monthly_import_count,
            quota_reset_at,
            allow_broker_sync,
            allow_web_search,
            allow_export_csv
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
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Permission service unavailable",
        )


# ------------------------------------------------------------------
# AI Access Permission
# ------------------------------------------------------------------

async def check_ai_quota(
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Enforces AI usage limits.
    """

    user_id = current_user["user_id"]
    user_profile = await _load_user_profile(user_id)

    # Daily message limit
    await QuotaManager.check_usage_limit(
        user_id=user_id,
        user_profile=user_profile,
        limit_key="daily_chat_msgs",
        current_usage_key="daily_chat_count",
        reset_key="last_chat_reset_at",
        reset_frequency="daily",
    )

    # Monthly token limit (fail-fast only)
    await QuotaManager.check_usage_limit(
        user_id=user_id,
        user_profile=user_profile,
        limit_key="monthly_ai_tokens_limit",
        current_usage_key="monthly_ai_tokens_used",
    )

    return user_profile


# ------------------------------------------------------------------
# CSV Import Permission
# ------------------------------------------------------------------

async def check_import_quota(
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Enforces CSV import limits.
    """

    user_id = current_user["user_id"]
    user_profile = await _load_user_profile(user_id)

    await QuotaManager.check_usage_limit(
        user_id=user_id,
        user_profile=user_profile,
        limit_key="monthly_csv_imports",
        current_usage_key="monthly_import_count",
        reset_key="quota_reset_at",
        reset_frequency="monthly",
    )

    return user_profile


# ------------------------------------------------------------------
# Feature Access Permissions
# ------------------------------------------------------------------

async def check_broker_sync_access(
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> None:
    user_profile = await _load_user_profile(current_user["user_id"])
    QuotaManager.check_feature_access(user_profile, "allow_broker_sync")


async def check_web_search_access(
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> None:
    user_profile = await _load_user_profile(current_user["user_id"])
    QuotaManager.check_feature_access(user_profile, "allow_web_search")


async def check_export_access(
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> None:
    user_profile = await _load_user_profile(current_user["user_id"])
    QuotaManager.check_feature_access(user_profile, "allow_export_csv")
