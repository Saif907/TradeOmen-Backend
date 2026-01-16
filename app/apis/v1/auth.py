# backend/app/apis/v1/auth.py
from fastapi import APIRouter, Depends, HTTPException
from typing import Dict, Any, Optional
from pydantic import BaseModel
import logging

from app.auth.dependency import get_current_user

# Import Supabase client safely

import os
from supabase import create_client, Client
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
    # 1. Get User ID (Supabase Auth usually puts the UUID in 'sub')
    user_id = current_user.get("sub") or current_user.get("user_id") or current_user.get("id")
    
    if not user_id:
        raise HTTPException(status_code=400, detail="User ID not found in session")

    # 2. Filter data
    update_data = {k: v for k, v in user_update.dict().items() if v is not None}
    
    if not update_data:
        return current_user

    # ✅ SAFEGUARD: Remove 'full_name' because your 'user_profiles' schema doesn't have it.
    # This prevents "column full_name does not exist" error.
    if "full_name" in update_data:
        update_data.pop("full_name")

    # If nothing left to update (e.g. only full_name was sent), just return
    if not update_data:
        return current_user

    logger.info(f"🔄 Updating 'user_profiles' for {user_id}: {update_data}")

    try:
        # 3. Perform Update
        # ✅ FIX: Use .eq("id", user_id) to match your DB schema primary key
        response = supabase.table("user_profiles").update(update_data).eq("id", user_id).execute()
        
        if not response.data:
            logger.warning(f"⚠️ Update succeeded but no row returned. Check if ID {user_id} exists in 'user_profiles'.")
            return {**current_user, **update_data}

        updated_profile = response.data[0]
        return {**current_user, **updated_profile}

    except Exception as e:
        logger.error(f"❌ DATABASE ERROR: {str(e)}")
        # Returns specific error details to help debugging
        raise HTTPException(status_code=500, detail=f"Database Update Failed: {str(e)}")