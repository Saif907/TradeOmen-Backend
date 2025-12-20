# backend/app/apis/v1/strategies.py

import logging
import time
from typing import List, Dict, Any
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from supabase import create_client, Client

from app.core.config import settings
from app.auth.dependency import get_current_user

# ✅ IMPORT CENTRALIZED SCHEMAS
from app.schemas import (
    StrategyCreate, 
    StrategyUpdate, 
    StrategyResponse, 
    PlanTier
)

router = APIRouter()
security = HTTPBearer()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------
# Services (Local Helpers)
# ---------------------------------------------------------------------

class PlanService:
    """
    Handles Latency/Caching for User Plans.
    Identical to the service in trades.py to maintain consistency.
    """
    _PLAN_CACHE: Dict[str, Dict[str, Any]] = {}
    CACHE_TTL = 60 

    @classmethod
    def get_user_plan(cls, user_id: str, supabase: Client) -> str:
        now = time.time()
        
        # 1. Check Cache
        cached = cls._PLAN_CACHE.get(user_id)
        if cached and cached["expires_at"] > now:
            return cached["plan"]

        # 2. DB Fetch
        try:
            # Matches 'user_profiles' table used in trades.py
            res = supabase.table("user_profiles").select("plan_tier").eq("id", user_id).single().execute()
            real_plan = (res.data.get("plan_tier") or "FREE").upper()
        except Exception as e:
            logger.error(f"Plan fetch failed for {user_id}: {e}")
            real_plan = "FREE"

        # 3. Update Cache
        cls._PLAN_CACHE[user_id] = {
            "plan": real_plan,
            "expires_at": now + cls.CACHE_TTL
        }
        return real_plan

# ---------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------

def get_authenticated_client(creds: HTTPAuthorizationCredentials = Depends(security)) -> Client:
    """
    Initializes Supabase Client with ANON key.
    The .auth() call elevates permissions to the specific user's level via RLS.
    """
    token = creds.credentials
    # ✅ SECURITY: Use ANON_KEY to respect RLS policies
    client = create_client(settings.SUPABASE_URL, settings.SUPABASE_ANON_KEY)
    client.postgrest.auth(token)
    return client

# ---------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------

@router.post("/", response_model=StrategyResponse, status_code=status.HTTP_201_CREATED)
def create_strategy(
    strategy: StrategyCreate,
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client)
):
    user_id = current_user["sub"]
    
    # 1. Fetch User Plan
    user_plan = PlanService.get_user_plan(user_id, supabase)
    
    # 2. Get Limits from Central Config
    # Default to FREE limits if plan not found in config
    plan_limits = settings.PLAN_LIMITS.get(user_plan, settings.PLAN_LIMITS["FREE"])
    max_strategies = plan_limits.get("strategies", 1)

    # 3. Check Current Count
    # We use count='exact', head=True to get the number without fetching data rows (Performance)
    res = supabase.table("strategies") \
        .select("id", count="exact", head=True) \
        .eq("user_id", user_id) \
        .execute()
    
    current_count = getattr(res, "count", 0) or 0

    # 4. Enforce Quota
    if current_count >= max_strategies:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=f"Strategy limit reached ({current_count}/{max_strategies}). Upgrade to add more."
        )

    # 5. Prepare Data
    data = strategy.model_dump()
    data["user_id"] = user_id
    # Explicit UTC to prevent Postgres timezone issues
    data["created_at"] = datetime.now(timezone.utc).isoformat()
    
    # -----------------------------------------------------------
    # TODO: AI Embeddings Hook
    # embedding = generate_embedding(f"{data['name']} {data.get('description')}")
    # data['embedding'] = embedding
    # -----------------------------------------------------------

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
    user_id = current_user["sub"]
    try:
        # Fetch strategies for user, newest first
        response = supabase.table("strategies")\
            .select("*")\
            .eq("user_id", user_id)\
            .order("created_at", desc=True)\
            .execute()
            
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
    # exclude_unset=True ensures we only send fields the user actually changed
    update_data = strategy.model_dump(exclude_unset=True)
    
    if not update_data:
        raise HTTPException(status_code=400, detail="No fields to update")

    update_data["updated_at"] = datetime.now(timezone.utc).isoformat()

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
        # RLS will ensure user can only delete their own
        # We assume ON DELETE CASCADE is set on the 'trades' foreign key in DB
        # If not, you might need to check for existing trades before deleting.
        response = supabase.table("strategies").delete().eq("id", strategy_id).execute()
        return None
    except Exception as e:
        logger.error(f"Error deleting strategy {strategy_id}: {e}")
        raise HTTPException(status_code=500, detail="Deletion failed")