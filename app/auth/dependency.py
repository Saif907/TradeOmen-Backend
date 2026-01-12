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
    
    INDUSTRY-GRADE FEATURE:
    - Implements JIT (Just-In-Time) Provisioning.
    - If a valid Supabase User exists but has no DB row, create it immediately.
    - Prevents 403 errors on new sign-ups if webhooks fail.
    """

    token = credentials.credentials

    # ------------------------------------------------------------------
    # 1. Verify JWT (Strict Security)
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
    email = auth_payload.get("email")
    
    # Extract metadata safely
    user_metadata = auth_payload.get("user_metadata", {})
    full_name = user_metadata.get("full_name") or user_metadata.get("name") or ""

    if not user_id:
        logger.warning("Authenticated token missing sub claim")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
        )

    # ------------------------------------------------------------------
    # 2. Fetch User Profile (With JIT Fallback)
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

        # [JIT PROVISIONING LOGIC START]
        if not user_profile:
            logger.info(f"JIT: User {user_id} missing in DB. Provisioning now...")
            
            # Create the missing profile on the fly
            insert_query = """
                INSERT INTO public.user_profiles (
                    id, 
                    email, 
                    full_name, 
                    active_plan_id, 
                    ai_chat_quota_used,
                    preferences
                ) VALUES ($1, $2, $3, 'free', 0, '{}')
                RETURNING id, active_plan_id, ai_chat_quota_used, preferences
            """
            try:
                user_profile = await db.fetch_one(
                    insert_query, 
                    user_id, 
                    email, 
                    full_name
                )
                logger.info(f"JIT: Successfully provisioned user {user_id}")
            except Exception as e:
                logger.error(f"JIT Provisioning failed for {user_id}: {e}")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to initialize user account"
                )
        # [JIT PROVISIONING LOGIC END]

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Database error loading user {user_id}: {e}")
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
        "email": email,
        "plan_id": user_profile["active_plan_id"],
        "ai_chat_quota_used": user_profile["ai_chat_quota_used"],
        "preferences": user_profile.get("preferences", {}),
        "auth_claims": auth_payload,
    }


async def get_current_active_user(
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Extension point for future checks (e.g., banning).
    """
    return current_user