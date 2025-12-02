# backend/app/apis/auth.py

from fastapi import APIRouter, Depends, HTTPException, status
from loguru import logger
from supabase import Client
from typing import Dict, Any

from app.auth.dependency import (
    AuthenticatedUser, 
    UserProfile, 
    DBClient, 
    FounderDBAccess
)
from app.libs.data_models import UserProfileUpdate
from app.libs.supabase_client import get_supabase_client # Needed for direct Depends if not using Annotated

router = APIRouter()

# --- Public Endpoint: Founder/Dev Access ---

@router.get("/admin/profile/{user_id}", response_model=Dict[str, Any], tags=["Founder Tools"])
async def get_any_user_profile_admin(
    user_id: str,
    db: FounderDBAccess # Corrected: Use Annotated type directly
):
    """
    [ADMIN ONLY] Retrieves any user profile using the Service Role Key.
    """
    logger.warning(f"ADMIN_ACCESS: Retrieving profile for user {user_id} via Service Key.")
    try:
        response = db.table('user_profiles').select('*').eq('id', user_id).single().execute()
        
        if not response.data:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"User profile {user_id} not found.")

        return response.data
    except Exception as e:
        logger.error(f"DB_ERROR: Admin retrieval failed for user {user_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to retrieve profile.")


# --- Authenticated Endpoints: User CRUD ---

@router.get("/users/me", response_model=Dict[str, Any], summary="Get my profile and plan status")
async def get_my_profile(
    profile: UserProfile # Corrected: Use Annotated type directly
):
    """
    Retrieves the currently authenticated user's profile.
    """
    logger.info(f"PROFILE_RETRIEVE: User {profile['id']} fetched profile.")
    return profile

@router.post("/users/initialize", status_code=status.HTTP_201_CREATED, summary="Initialize profile after Supabase signup")
async def initialize_user_profile(
    user: AuthenticatedUser, # Corrected
    db: DBClient             # Corrected: Use Annotated type directly
):
    """
    Creates the initial user_profiles entry.
    """
    try:
        existing = db.table('user_profiles').select('id').eq('id', str(user.user_id)).maybe_single().execute()
        if existing.data:
            logger.warning(f"PROFILE_EXISTS: Attempted initialization for existing user {user.user_id}.")
            return {"detail": "Profile already exists."}

        data_to_insert = {
            'id': str(user.user_id),
            'active_plan_id': 'FREE',
            'region_code': 'US', 
            'default_currency': 'USD',
            'ai_chat_quota_used': 0,
            'consent_ai_training': False,
        }
        
        db.table('user_profiles').insert(data_to_insert).execute()
        logger.success(f"PROFILE_CREATE: Successfully initialized profile for new user {user.user_id}.")
        
        return {"detail": "Profile initialized successfully."}

    except Exception as e:
        logger.error(f"DB_ERROR: Profile initialization failed for user {user.user_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to initialize user profile.")


@router.put("/users/me", response_model=Dict[str, Any], summary="Update my profile settings and consent")
async def update_my_profile(
    update_data: UserProfileUpdate,
    profile: UserProfile, # Corrected
    db: DBClient          # Corrected
):
    """
    Allows user to update settings.
    """
    data = update_data.model_dump(exclude_unset=True, exclude_none=True)
    
    if not data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No valid fields provided for update.")

    try:
        response = db.table('user_profiles').update(data).eq('id', profile['id']).execute()
        
        if not response.data:
            logger.warning(f"PROFILE_UPDATE_FAIL: No rows updated for user {profile['id']}.")
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile update failed or profile not found.")
            
        logger.success(f"PROFILE_UPDATE: User {profile['id']} updated fields: {', '.join(data.keys())}.")
        
        return response.data[0] 

    except Exception as e:
        logger.error(f"DB_ERROR: Profile update failed for user {profile['id']}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Database error during profile update.")