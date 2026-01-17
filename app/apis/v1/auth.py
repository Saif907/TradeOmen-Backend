# backend/app/apis/v1/auth.py
from fastapi import APIRouter, Depends, HTTPException
from typing import Dict, Any, Optional
from pydantic import BaseModel
import logging

# 1. IMPORT THE CACHE directly from the dependency file
from app.auth.dependency import get_current_user, _USER_CACHE

import os
from supabase import create_client, Client

# Initialize Supabase Client
url = os.environ.get("SUPABASE_URL")
key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
supabase: Client = create_client(url, key)

router = APIRouter()
logger = logging.getLogger("uvicorn.error")

class UserUpdate(BaseModel):
    full_name: Optional[str] = None
    preferences: Optional[Dict[str, Any]] = None

@router.get("/me", response_model=Dict[str, Any])
async def read_users_me(current_user: Dict[str, Any] = Depends(get_current_user)):
    return current_user

@router.patch("/me", response_model=Dict[str, Any])
async def update_user_me(
    user_update: UserUpdate, 
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    # 1. Get User ID
    user_id = current_user.get("sub") or current_user.get("user_id") or current_user.get("id")
    
    if not user_id:
        raise HTTPException(status_code=400, detail="User ID not found in session")

    # 2. Filter data
    update_data = {k: v for k, v in user_update.dict().items() if v is not None}
    
    if not update_data:
        return current_user

    # ✅ SAFEGUARD: Remove 'full_name' if schema doesn't support it
    if "full_name" in update_data:
        update_data.pop("full_name")

    # If nothing left to update, just return
    if not update_data:
        return current_user

    logger.info(f"🔄 Updating 'user_profiles' for {user_id}: {update_data}")

    try:
        # 3. Perform Update
        response = supabase.table("user_profiles").update(update_data).eq("id", user_id).execute()
        
        # ---------------------------------------------------------
        # 4. CACHE INVALIDATION (The Fix)
        # ---------------------------------------------------------
        # Immediately remove the user from cache. 
        # The next request will be forced to fetch the new data from DB.
        if user_id in _USER_CACHE:
            del _USER_CACHE[user_id]
            logger.info(f"🧹 Cache cleared for user {user_id}")
        # ---------------------------------------------------------

        if not response.data:
            logger.warning(f"⚠️ Update succeeded but no row returned. Check if ID {user_id} exists in 'user_profiles'.")
            return {**current_user, **update_data}

        updated_profile = response.data[0]
        return {**current_user, **updated_profile}

    except Exception as e:
        logger.error(f"❌ DATABASE ERROR: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Database Update Failed: {str(e)}")