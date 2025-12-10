# backend/app/apis/v1/strategies.py
import logging
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field
from datetime import datetime
from supabase import create_client, Client

from app.core.config import settings
from app.auth.dependency import get_current_user

# --- Configuration & Logging ---
router = APIRouter()
security = HTTPBearer()
logger = logging.getLogger(__name__)

# --- Pydantic Models ---

class StrategyBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = None
    emoji: str = Field("📈", max_length=4)
    color_hex: str = Field("#8b5cf6", pattern="^#([A-Fa-f0-9]{6}|[A-Fa-f0-9]{3})$")
    style: Optional[str] = Field(None, description="Day Trading, Swing, etc.")
    instrument_types: List[str] = []
    # Rules is a flexible Dictionary to support custom groups
    # Example: {"Market": ["Trend Up"], "Psychology": ["No FOMO"]}
    rules: Dict[str, List[str]] = Field(default_factory=dict)
    track_missed_trades: bool = True

class StrategyCreate(StrategyBase):
    pass

class StrategyUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    emoji: Optional[str] = None
    color_hex: Optional[str] = None
    style: Optional[str] = None
    instrument_types: Optional[List[str]] = None
    rules: Optional[Dict[str, List[str]]] = None
    track_missed_trades: Optional[bool] = None

class StrategyResponse(StrategyBase):
    id: str # UUID
    user_id: str
    created_at: datetime
    updated_at: datetime

# --- Dependency: Authenticated Supabase Client ---

def get_authenticated_client(creds: HTTPAuthorizationCredentials = Depends(security)) -> Client:
    """
    Creates a Supabase client authenticating AS THE USER.
    This ensures RLS policies are automatically enforced.
    """
    token = creds.credentials
    # Initialize with Service Role Key but Scope to User Token
    client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)
    client.postgrest.auth(token)
    return client

# --- Endpoints ---

@router.post("/", response_model=StrategyResponse, status_code=status.HTTP_201_CREATED)
def create_strategy(
    strategy: StrategyCreate,
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client)
):
    """
    Create a new trading strategy/playbook.
    """
    # OPTIMIZATION: Use the locally decoded JWT user_id instead of calling Supabase Auth API
    user_id = current_user["sub"]
    
    logger.info(f"User {user_id} creating strategy: {strategy.name}")
        
    data = strategy.dict()
    data["user_id"] = user_id
    
    try:
        # Insert into Supabase
        response = supabase.table("strategies").insert(data).execute()
        
        if not response.data:
            logger.error(f"Strategy creation returned no data for user {user_id}")
            raise HTTPException(status_code=400, detail="Failed to create strategy")
            
        return response.data[0]
        
    except Exception as e:
        logger.error(f"DB Error creating strategy: {e}")
        raise HTTPException(status_code=400, detail=f"Database Error: {str(e)}")

@router.get("/", response_model=List[StrategyResponse])
def get_strategies(
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client)
):
    """
    List all strategies for the current user.
    RLS ensures users only see their own strategies.
    """
    try:
        response = supabase.table("strategies")\
            .select("*")\
            .order("name", desc=False)\
            .execute()
            
        return response.data
    except Exception as e:
        logger.error(f"Error fetching strategies for {current_user['sub']}: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch strategies")

@router.get("/{strategy_id}", response_model=StrategyResponse)
def get_strategy(
    strategy_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client)
):
    """
    Get a specific strategy by ID.
    """
    try:
        response = supabase.table("strategies").select("*").eq("id", strategy_id).execute()
        
        if not response.data:
            raise HTTPException(status_code=404, detail="Strategy not found")
            
        return response.data[0]
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching strategy {strategy_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@router.patch("/{strategy_id}", response_model=StrategyResponse)
def update_strategy(
    strategy_id: str,
    strategy: StrategyUpdate,
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client)
):
    """
    Update a strategy.
    """
    # Filter out None values to only update fields that were sent
    update_data = {k: v for k, v in strategy.dict().items() if v is not None}
    
    if not update_data:
        raise HTTPException(status_code=400, detail="No fields to update")

    update_data["updated_at"] = datetime.now().isoformat()

    try:
        response = supabase.table("strategies")\
            .update(update_data)\
            .eq("id", strategy_id)\
            .execute()
        
        if not response.data:
            raise HTTPException(status_code=404, detail="Strategy not found or not authorized")
            
        logger.info(f"Strategy {strategy_id} updated by {current_user['sub']}")
        return response.data[0]
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating strategy {strategy_id}: {e}")
        raise HTTPException(status_code=500, detail="Update failed")

@router.delete("/{strategy_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_strategy(
    strategy_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client)
):
    """
    Delete a strategy.
    """
    try:
        response = supabase.table("strategies").delete().eq("id", strategy_id).execute()
        
        # Supabase delete returns the rows deleted. If empty, it means nothing happened (not found/auth).
        if not response.data:
            raise HTTPException(status_code=404, detail="Strategy not found or not authorized")
        
        logger.info(f"Strategy {strategy_id} deleted by {current_user['sub']}")
        return None
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting strategy {strategy_id}: {e}")
        raise HTTPException(status_code=500, detail="Deletion failed")