from fastapi import APIRouter, Depends, HTTPException, status, Body
from typing import Dict, Any
from supabase import Client
from postgrest.base_request_builder import SingleAPIResponse

from ..libs import schemas
from ..auth.dependencies import AuthUser, ServiceSupabaseClient # <-- Import Service Client

# Initialize the router
router = APIRouter()

class SupabaseLoginPayload(schemas.BaseModel):
    pass

@router.post(
    "/login",
    response_model=schemas.UserInDB,
)
async def login_and_verify_session(
    current_user: schemas.UserInDB = AuthUser,
    # Use the SERVICE ROLE client, which can bypass RLS to force profile creation
    supabase_service_client: Client = ServiceSupabaseClient,
    payload: SupabaseLoginPayload = Body(None) 
):
    """
    Verifies JWT and creates the public.user_profiles row if it doesn't exist.
    This function is IDEMPOTENT (safe to call repeatedly) and guarantees 
    the Foreign Key dependency is met.
    """
    user_id = current_user.user_id
    
    # 1. Prepare data for insertion/upsert
    profile_data = {
        'user_id': user_id,
        # 'plan': current_user.plan.value # Optional: set initial plan here
    }
    
    try:
        # 2. Perform UPSERT: Insert OR Update on Conflict (user_id)
        # This guarantees the row exists after the call and will not throw an error
        # if the row already exists (solving the race condition).
        response: SingleAPIResponse = (
            supabase_service_client.table("user_profiles")
            .upsert(profile_data, on_conflict="user_id") 
            .execute()
        )
        
        # 3. Return the authenticated user object, confirming the profile exists.
        return current_user

    except Exception as e:
        # If the upsert fails for reasons other than expected conflict
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Fatal error during profile creation/sync: {e}",
        )


@router.get(
    "/user",
    response_model=schemas.UserInDB,
)
def get_user_profile(
    current_user: schemas.UserInDB = AuthUser
):
    """Returns the currently authenticated user's profile and plan details."""
    return current_user