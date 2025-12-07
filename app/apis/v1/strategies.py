# backend/app/apis/v1/strategies.py
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field
from datetime import datetime
from supabase import create_client, Client

from app.core.config import settings

router = APIRouter()
security = HTTPBearer()

# --- Pydantic Models ---

class StrategyBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = None
    emoji: str = Field("📈", max_length=4)
    color_hex: str = Field("#8b5cf6", pattern="^#([A-Fa-f0-9]{6}|[A-Fa-f0-9]{3})$")
    style: Optional[str] = Field(None, description="Day Trading, Swing, etc.")
    instrument_types: List[str] = []
    # CHANGED: Rules is now a flexible Dictionary to support custom groups
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
    supabase: Client = Depends(get_authenticated_client)
):
    """
    Create a new trading strategy/playbook.
    """
    # Get user_id securely from the token
    user_resp = supabase.auth.get_user()
    if not user_resp.user:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    data = strategy.dict()
    data["user_id"] = user_resp.user.id
    
    # Insert into Supabase
    response = supabase.table("strategies").insert(data).execute()
    
    if not response.data:
        raise HTTPException(status_code=400, detail="Failed to create strategy")
        
    return response.data[0]

@router.get("/", response_model=List[StrategyResponse])
def get_strategies(
    supabase: Client = Depends(get_authenticated_client)
):
    """
    List all strategies for the current user.
    RLS ensures users only see their own strategies.
    """
    response = supabase.table("strategies")\
        .select("*")\
        .order("name", desc=False)\
        .execute()
        
    return response.data

@router.get("/{strategy_id}", response_model=StrategyResponse)
def get_strategy(
    strategy_id: str,
    supabase: Client = Depends(get_authenticated_client)
):
    """
    Get a specific strategy by ID.
    """
    response = supabase.table("strategies").select("*").eq("id", strategy_id).execute()
    
    if not response.data:
        raise HTTPException(status_code=404, detail="Strategy not found")
        
    return response.data[0]

@router.patch("/{strategy_id}", response_model=StrategyResponse)
def update_strategy(
    strategy_id: str,
    strategy: StrategyUpdate,
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

    response = supabase.table("strategies")\
        .update(update_data)\
        .eq("id", strategy_id)\
        .execute()
    
    if not response.data:
        raise HTTPException(status_code=404, detail="Strategy not found or not authorized")
        
    return response.data[0]

@router.delete("/{strategy_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_strategy(
    strategy_id: str,
    supabase: Client = Depends(get_authenticated_client)
):
    """
    Delete a strategy.
    """
    response = supabase.table("strategies").delete().eq("id", strategy_id).execute()
    
    # Supabase delete returns the rows deleted. If empty, it means nothing happened (not found/auth).
    if not response.data:
        raise HTTPException(status_code=404, detail="Strategy not found or not authorized")
        
    return None