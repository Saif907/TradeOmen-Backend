# backend/app/auth/dependency.py

import logging
import time
from typing import Dict, Any, Optional

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

# ✅ 1. IMPORT PERFORMANCE MONITOR
from app.services.performance_monitor import PerformanceMonitor

logger = logging.getLogger("tradeomen.auth.dependency")

security = HTTPBearer(auto_error=True)

# ------------------------------------------------------------------
# CACHE CONFIGURATION
# ------------------------------------------------------------------
# Simple in-memory cache to reduce DB hits on every request.
# Format: { "user_uuid": { "data": {...}, "expires_at": 1700000000 } }
_USER_CACHE: Dict[str, Dict[str, Any]] = {}
CACHE_TTL_SECONDS = 180

# ✅ NEW HELPER: Write-Through Cache Update
# This allows QuotaManager to sync updates immediately, preventing stale data.
def update_user_cache(user_id: str, updates: Dict[str, Any]) -> None:
    """
    Updates the in-memory cache with fresh values (Write-Through).
    This prevents stale data from allowing users to bypass quotas.
    """
    if user_id in _USER_CACHE:
        # Update only the fields provided, keep the rest (like plan_id) intact
        _USER_CACHE[user_id]["data"].update(updates)

async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> Dict[str, Any]:
    """
    Authenticates request and returns a normalized user context.
    
    OPTIMIZATION:
    - Implements 60s in-memory caching for user profiles.
    - Drastically reduces DB reads for frequent API calls.
    - Implements JIT (Just-In-Time) Provisioning for new users.
    - Tracks Cache Effectiveness via PerformanceMonitor.
    """

    token = credentials.credentials

    # ------------------------------------------------------------------
    # 1. Verify JWT (Strict Security - CPU Bound, Fast)
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
    
    if not user_id:
        logger.warning("Authenticated token missing sub claim")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
        )

    # ------------------------------------------------------------------
    # 2. Check Cache (Optimization)
    # ------------------------------------------------------------------
    now = time.time()
    cached_entry = _USER_CACHE.get(user_id)
    
    if cached_entry and cached_entry["expires_at"] > now:
        # ✅ CACHE HIT: Record stat and return immediately
        await PerformanceMonitor.record_auth_cache(hit=True)
        user_profile = cached_entry["data"]
    else:
        # ✅ CACHE MISS: Record stat and fetch from DB
        await PerformanceMonitor.record_auth_cache(hit=False)
        try:
            # ✅ OPTIMIZATION: Fetch ALL counters in one go.
            # This eliminates the need for permissions.py to re-query the DB.
            query = """
                SELECT
                    id,
                    active_plan_id,
                    daily_chat_count,
                    last_chat_reset_at,
                    monthly_ai_tokens_used,
                    monthly_import_count,
                    quota_reset_at,
                    preferences
                FROM public.user_profiles
                WHERE id = $1
            """
            row = await db.fetch_one(query, user_id)

            # [JIT PROVISIONING LOGIC START]
            if not row:
                logger.info(f"JIT: User {user_id} missing in DB. Provisioning now...")
                
                # Extract metadata for creation
                user_metadata = auth_payload.get("user_metadata", {})
                full_name = user_metadata.get("full_name") or user_metadata.get("name") or ""
                
                # Create the missing profile on the fly
                # Defaults: chat_count=0, tokens=0 via DB schema defaults
                insert_query = """
                    INSERT INTO public.user_profiles (
                        id, 
                        email, 
                        full_name, 
                        active_plan_id, 
                        preferences
                    ) VALUES ($1, $2, $3, 'free', '{}')
                    RETURNING id, active_plan_id, preferences
                """
                try:
                    row = await db.fetch_one(
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

            # Convert DB Record to Dict for caching
            user_profile = dict(row)
            
            # Store in Cache
            _USER_CACHE[user_id] = {
                "data": user_profile,
                "expires_at": now + CACHE_TTL_SECONDS
            }

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
        
        # ✅ Plan Details (Mapped correctly for trades.py)
        "plan_id": user_profile.get("active_plan_id"),
        "active_plan_id": user_profile.get("active_plan_id"),
        "plan_tier": user_profile.get("active_plan_id"),  # Alias for compatibility
        
        # ✅ Usage Counters (Passed to QuotaManager to avoid extra DB calls)
        "daily_chat_count": user_profile.get("daily_chat_count", 0),
        "last_chat_reset_at": user_profile.get("last_chat_reset_at"),
        "monthly_ai_tokens_used": user_profile.get("monthly_ai_tokens_used", 0),
        "monthly_import_count": user_profile.get("monthly_import_count", 0),
        "quota_reset_at": user_profile.get("quota_reset_at"),
        
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