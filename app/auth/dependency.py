# backend/app/auth/dependency.py
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from app.auth.security import AuthSecurity
from app.core.database import db
import logging

logger = logging.getLogger(__name__)

# This security scheme extracts the Bearer token from the Authorization header
security = HTTPBearer()

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """
    FastAPI Dependency to authenticate users via Supabase JWT.
    
    Flow:
    1. Extracts Bearer token from header.
    2. Validates signature locally (fast) via AuthSecurity.
    3. Fetches user profile from DB (asyncpg) to get plan/quota info.
    
    Returns:
        dict: The user profile combined with auth claims.
    """
    token = credentials.credentials
    
    # 1. Verify JWT Signature & Claims
    auth_payload = AuthSecurity.verify_token(token)
    user_id = auth_payload.get("sub")
    
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing user ID (sub)",
        )

    # 2. Fetch User Profile from Database (High Performance Async)
    # We use the raw asyncpg pool for maximum speed
    try:
        if db.pool:
            query = """
                SELECT id, full_name, active_plan_id, ai_chat_quota_used, preferences
                FROM public.profiles 
                WHERE id = $1
            """
            user_profile = await db.fetch_one(query, user_id)
            
            if user_profile:
                # Merge DB profile data (Plan, Quotas) with Auth token data
                return {**auth_payload, **dict(user_profile)}
            
            logger.warning(f"User {user_id} authenticated but has no profile table entry.")
            
        # Fallback: Return just the auth payload if DB lookup fails or profile missing
        return auth_payload

    except Exception as e:
        logger.error(f"Error fetching user profile context: {e}")
        # Still return auth_payload so the request doesn't fail completely, 
        # but user might be treated as 'free' tier by default logic.
        return auth_payload

async def get_current_active_user(current_user: dict = Depends(get_current_user)):
    """
    Dependency wrapper to add future checks (e.g., is_banned, email_verified).
    """
    return current_user