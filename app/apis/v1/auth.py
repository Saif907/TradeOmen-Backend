# backend/app/apis/v1/auth.py
from fastapi import APIRouter, Depends
from typing import Dict, Any

from app.auth.dependency import get_current_user

router = APIRouter()

@router.get("/me", response_model=Dict[str, Any])
async def read_users_me(current_user: Dict[str, Any] = Depends(get_current_user)):
    """
    Test endpoint to validate the Bearer token.
    Returns the decoded User Profile + Auth Claims.
    
    This is used by the frontend to confirm the backend recognizes the user session.
    """
    return current_user