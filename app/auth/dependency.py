import logging
import time
from typing import Dict, Any, Optional
from cachetools import TTLCache 

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
from app.services.performance_monitor import PerformanceMonitor

logger = logging.getLogger("tradeomen.auth.dependency")

security = HTTPBearer(auto_error=True)

# ------------------------------------------------------------------
# CACHE CONFIGURATION
# ------------------------------------------------------------------
# Use TTLCache to prevent Memory Leaks.
# - maxsize=10000: Stores max 10k active users in RAM.
# - ttl=180: Automatically deletes keys after 3 minutes.
_USER_CACHE = TTLCache(maxsize=10000, ttl=180)

# ✅ NEW: Function to force-clear cache when Admin updates a user
def invalidate_user_cache(user_id: str) -> None:
    """
    Removes a user from the RAM cache. 
    This forces the next request to fetch fresh data from the DB.
    """
    if user_id in _USER_CACHE:
        try:
            del _USER_CACHE[user_id]
            logger.info(f"[CACHE] 🗑️ Invalidated cache for user {user_id}")
        except KeyError:
            pass # Key might have expired naturally just now

def update_user_cache(user_id: str, updates: Dict[str, Any]) -> None:
    """
    Updates the in-memory cache with fresh values (Write-Through).
    """
    if user_id in _USER_CACHE:
        current_data = _USER_CACHE[user_id]
        current_data.update(updates)
        _USER_CACHE[user_id] = current_data

async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> Dict[str, Any]:
    token = credentials.credentials

    # 1. Verify JWT
    try:
        auth_payload = AuthSecurity.verify_token(token)
    except (ExpiredTokenError, InvalidTokenError, AuthenticationError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except InvalidRoleError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied",
        )

    user_id = auth_payload.get("sub")
    email = auth_payload.get("email")

    if not user_id:
        raise HTTPException(status_code=401, detail="Token missing sub")

    # 2. Check Cache
    user_profile = {}
    
    if user_id in _USER_CACHE:
        await PerformanceMonitor.record_auth_cache(hit=True)
        user_profile = _USER_CACHE[user_id]
        # logger.debug(f"[AUTH DEBUG] Cache HIT for {user_id}")
    else:
        await PerformanceMonitor.record_auth_cache(hit=False)
        try:
            # ✅ UPDATED: Explicitly select 'plan_tier'
            query = """
                SELECT id, role, active_plan_id, daily_chat_count, 
                       last_chat_reset_at, monthly_ai_tokens_used, 
                       monthly_import_count, quota_reset_at, preferences, plan_tier
                FROM public.user_profiles WHERE id = $1
            """
            row = await db.fetch_one(query, user_id)

            # [JIT PROVISIONING]
            if not row:
                logger.info(f"[AUTH DEBUG] User {user_id} not found in DB. Attempting JIT...")
                user_metadata = auth_payload.get("user_metadata", {})
                full_name = user_metadata.get("full_name", "")

                insert_query = """
                    INSERT INTO public.user_profiles (
                        id, email, full_name, active_plan_id, plan_tier, preferences
                    ) VALUES ($1, $2, $3, 'free', 'FREE', '{}')
                    ON CONFLICT (id) DO NOTHING
                    RETURNING id, role, active_plan_id, plan_tier, preferences
                """
                row = await db.fetch_one(insert_query, user_id, email, full_name)
                
                if not row:
                    row = await db.fetch_one(query, user_id)

            if row:
                user_profile = dict(row)
                
                # Update Cache
                _USER_CACHE[user_id] = user_profile
                logger.info(f"[AUTH DEBUG] DB Fetch Success. User: {user_id} | Plan: {user_profile.get('plan_tier')}")
            else:
                logger.error(f"[AUTH DEBUG] Failed to fetch or create user profile for {user_id}")
                raise HTTPException(status_code=500, detail="Profile load failed")

        except Exception as e:
            logger.exception(f"[AUTH DEBUG] DB Error for {user_id}: {e}")
            raise HTTPException(status_code=503, detail="Service unavailable")

    # 3. Determine Final Role
    db_role = user_profile.get("role")
    jwt_role = auth_payload.get("role")
    final_role = db_role or jwt_role
    
    if final_role == "authenticated":
        logger.warning(f"⚠️ [AUTH WARNING] Role resolved to 'authenticated'. DB Role: {db_role}")

    return {
        "user_id": user_profile["id"],
        "role": final_role,
        "email": email,
        "plan_id": user_profile.get("active_plan_id"),
        "active_plan_id": user_profile.get("active_plan_id"),
        # ✅ UPDATED: Prefer explicit plan_tier column
        "plan_tier": user_profile.get("plan_tier") or user_profile.get("active_plan_id"),
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
    return current_user