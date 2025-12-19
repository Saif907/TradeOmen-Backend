# backend/app/apis/v1/strategies.py
import logging
from typing import List, Optional, Dict, Any
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field, field_validator
from supabase import create_client, Client

from app.core.config import settings
from app.auth.dependency import get_current_user
# 👇 Import QuotaManager
from app.services.quota_manager import QuotaManager

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
    id: str
    user_id: str
    created_at: datetime
    updated_at: Optional[datetime] = None 

# --- Dependency ---

def get_authenticated_client(creds: HTTPAuthorizationCredentials = Depends(security)) -> Client:
    token = creds.credentials
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
    user_id = current_user["sub"]
    plan_tier = current_user.get("plan_tier", "FREE")

    # -----------------------------------------------------------
    # 1. QUOTA CHECK: Max Strategies
    # -----------------------------------------------------------
    if plan_tier != "FOUNDER":
        # Fetch limits
        limits = QuotaManager.get_limits(plan_tier)
        max_strategies = limits.get("max_strategies", 1)

        # Count existing strategies
        # We use count="exact" and head=True to avoid fetching actual data (faster)
        res = supabase.table("strategies") \
            .select("id", count="exact", head=True) \
            .eq("user_id", user_id) \
            .execute()
        
        current_count = getattr(res, "count", 0) or 0

        if current_count >= max_strategies:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=f"Strategy limit reached ({current_count}/{max_strategies}). Upgrade to add more."
            )
    # -----------------------------------------------------------

    data = strategy.dict()
    data["user_id"] = user_id
    
    try:
        response = supabase.table("strategies").insert(data).execute()
        if not response.data:
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
    try:
        response = supabase.table("strategies").select("*").order("name", desc=False).execute()
        return response.data
    except Exception as e:
        logger.error(f"Error fetching strategies: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch strategies")

@router.get("/{strategy_id}", response_model=StrategyResponse)
def get_strategy(
    strategy_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client)
):
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
    update_data = {k: v for k, v in strategy.dict().items() if v is not None}
    if not update_data:
        raise HTTPException(status_code=400, detail="No fields to update")

    update_data["updated_at"] = datetime.now().isoformat()

    try:
        response = supabase.table("strategies").update(update_data).eq("id", strategy_id).execute()
        if not response.data:
            raise HTTPException(status_code=404, detail="Strategy not found or not authorized")
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
    try:
        response = supabase.table("strategies").delete().eq("id", strategy_id).execute()
        if not response.data:
            raise HTTPException(status_code=404, detail="Strategy not found")
        return None
    except Exception as e:
        logger.error(f"Error deleting strategy {strategy_id}: {e}")
        raise HTTPException(status_code=500, detail="Deletion failed")