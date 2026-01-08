# backend/app/auth/dependency.py

import logging
from typing import Dict, Any

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.auth.security import (
    AuthSecurity,
    ExpiredTokenError,
    InvalidTokenError,
    InvalidRoleError,
    AuthenticationError,
)
from app.core.database import db

logger = logging.getLogger("tradeomen.auth.dependency")

security = HTTPBearer(auto_error=True)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> Dict[str, Any]:
    """
    Authenticates request and returns a normalized user context.

    Guarantees:
    - Valid JWT
    - Authenticated Supabase user
    - Known user_id
    """

    token = credentials.credentials

    # ------------------------------------------------------------------
    # 1. Verify JWT
    # ------------------------------------------------------------------
    try:
        auth_payload = AuthSecurity.verify_token(token)
    except ExpiredTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except InvalidRoleError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied",
        )
    except InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except AuthenticationError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication failed",
        )

    user_id = auth_payload.get("sub")
    if not user_id:
        logger.warning("Authenticated token missing sub claim")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
        )

    # ------------------------------------------------------------------
    # 2. Fetch User Profile (STRICT)
    # ------------------------------------------------------------------
    try:
        query = """
            SELECT
                id,
                active_plan_id,
                ai_chat_quota_used,
                preferences
            FROM public.user_profiles
            WHERE id = $1
        """

        user_profile = await db.fetch_one(query, user_id)

        if not user_profile:
            logger.warning("Authenticated user missing profile", extra={"user_id": user_id})
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="User profile not found",
            )

    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to load user profile", extra={"user_id": user_id})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="User context unavailable",
        )

    # ------------------------------------------------------------------
    # 3. Normalized User Context
    # ------------------------------------------------------------------
    return {
        "user_id": user_profile["id"],
        "role": auth_payload.get("role"),
        "email": auth_payload.get("email"),
        "plan_id": user_profile["active_plan_id"],
        "ai_chat_quota_used": user_profile["ai_chat_quota_used"],
        "preferences": user_profile["preferences"],
        "auth_claims": auth_payload,  # kept for internal use only
    }


async def get_current_active_user(
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Extension point for future checks:
    - is_banned
    - email_verified
    - account_locked
    """
    return current_user
