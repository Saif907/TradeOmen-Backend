# backend/app/auth/dependency.py

import os
from typing import Annotated, Optional
from fastapi import Header, Depends, HTTPException, status
from loguru import logger
from supabase import Client

from app.auth.jwt_handler import decode_and_validate_jwt, JWTAuthError
from app.libs.supabase_client import get_supabase_client, get_founder_db_access
from app.libs.data_models import UserToken
from app.libs.config import settings, PLAN_TIERS 

# --- Plan Feature Mapping (Freemium Model Enforcement) ---

REQUIRED_PLANS = {
    "AI_CHAT": ["BASIC", "PRO"],
    "ADVANCED_ANALYTICS": ["PRO"],
    "BULK_IMPORT": ["BASIC", "PRO"],
    "BROKER_INTEGRATION": ["PRO"],
    "STRATEGY_CREATE": ["FREE", "BASIC", "PRO"],
    "READ_TRADES_FULL": ["FREE", "BASIC", "PRO"],
    "CREATE_TRADE_MANUAL": ["FREE", "BASIC", "PRO"],
}

# --- Dependencies for Authentication ---

def get_auth_token(authorization: Optional[str] = Header(None)) -> str:
    """Extracts the JWT token from the Authorization header."""
    if authorization is None:
        raise JWTAuthError(detail="Authorization header missing.")
        
    try:
        scheme, token = authorization.split()
    except ValueError:
        raise JWTAuthError(detail="Invalid Authorization header format. Expected 'Bearer <token>'.")
        
    if scheme.lower() != "bearer":
        raise JWTAuthError(detail="Authorization scheme must be 'Bearer'.")
        
    return token

async def get_current_user(token: str = Depends(get_auth_token)) -> UserToken:
    """
    (Super Fast/Async) Decodes and validates the JWT and returns the user's UUID (tenant ID).
    """
    return await decode_and_validate_jwt(token)

AuthenticatedUser = Annotated[UserToken, Depends(get_current_user)]
DBClient = Annotated[Client, Depends(get_supabase_client)]

# --- Dependencies for Founder/Dev Access ---

async def founder_db_access(
    user: AuthenticatedUser,
    db: Client = Depends(get_founder_db_access)
):
    """
    (Founder Access) Grants RLS-bypassing database access to the developer/founder.
    """
    if settings.ENVIRONMENT != "development":
        logger.warning(f"SECURITY ALERT: Founder access attempted in production by user {user.user_id}")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Founder access restricted outside development.")
    
    return db

FounderDBAccess = Annotated[Client, Depends(founder_db_access)]


# --- Dependencies for Subscription & Quota Enforcement ---

async def get_user_profile(user: AuthenticatedUser, db: Client = Depends(get_supabase_client)) -> dict:
    """
    (RLS Enforced) Fetches the user's profile (plan, quota, consent) for the current request.
    """
    try:
        response = db.table('user_profiles').select('*').eq('id', user.user_id).single().execute()
        
        if not response.data:
            logger.error(f"DB_ERROR: Profile not found for user {user.user_id}.")
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User profile is not initialized.")

        return response.data

    except Exception as e:
        logger.error(f"DB_ERROR: Failed to fetch profile for authorization: {e}")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database dependency failed.")


UserProfile = Annotated[dict, Depends(get_user_profile)]


def requires_plan(feature_key: str):
    """
    (Freemium Gate) Factory function for plan-based access control.
    """
    def check_plan_access(profile: UserProfile = Depends(get_user_profile)):
        user_plan = profile.get("active_plan_id", "FREE").upper()
        required_plans = [p.upper() for p in settings.REQUIRED_PLANS.get(feature_key, [])]
        
        if not required_plans:
            logger.warning(f"AUTH_PLAN_ERROR: Feature '{feature_key}' has no defined access plans.")
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Feature access denied due to server configuration.")
        
        if user_plan not in required_plans:
            logger.warning(f"AUTH_DENIED: User {profile['id']} ({user_plan}) denied access to paid feature {feature_key}.")
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=f"Feature '{feature_key}' requires an upgrade to one of the following plans: {', '.join(required_plans)}."
            )
        return True

    # FIX: Return the inner function directly
    return check_plan_access


def check_ai_quota(quota_limit: int):
    """
    (Fair Use Gate) Factory function to check AI usage quota.
    """
    def check_quota_limit(profile: UserProfile = Depends(get_user_profile)):
        user_plan = profile.get("active_plan_id", "FREE").upper()
        quota_used = profile.get("ai_chat_quota_used", 0)
        
        if user_plan == "PRO":
            return True
            
        if quota_used >= quota_limit:
            logger.warning(f"QUOTA_HIT: User {profile['id']} exceeded AI quota ({quota_used}/{quota_limit}).")
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail="AI usage quota exceeded. Please upgrade or wait for the quota reset."
            )
        return True

    # FIX: Return the inner function directly
    return check_quota_limit


def check_ai_consent(feature_key: str):
    """
    (Legal/Consent Gate) Factory function to enforce explicit consent for data use.
    """
    def check_consent_status(profile: UserProfile = Depends(get_user_profile)):
        consent_given = profile.get("consent_ai_training", False)
        user_plan = profile.get("active_plan_id").upper()
        
        if user_plan in ["BASIC", "PRO"] and not consent_given:
            logger.warning(f"CONSENT_REQUIRED: User {profile['id']} denied access to {feature_key} due to missing AI consent.")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access to this premium feature requires explicit consent to use your anonymized data for model training (per our Data Use Policy)."
            )
        return True
        
    # FIX: Return the inner function directly
    return check_consent_status