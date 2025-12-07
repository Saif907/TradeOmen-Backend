# backend/app/apis/v1/trades.py
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import List, Optional
from pydantic import BaseModel, Field, validator
from datetime import datetime
from supabase import create_client, Client

from app.core.config import settings

router = APIRouter()
security = HTTPBearer()

# --- Pydantic Models (Validation) ---

class TradeBase(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=20)
    direction: str = Field(..., pattern="^(Long|Short)$")
    status: str = Field("Closed", pattern="^(Open|Closed|Pending)$")
    entry_price: float = Field(..., gt=0)
    exit_price: Optional[float] = Field(None, gt=0)
    quantity: float = Field(..., gt=0)
    entry_date: datetime
    exit_date: Optional[datetime] = None
    fees: float = Field(0.0, ge=0)
    notes: Optional[str] = None
    strategy_id: Optional[int] = None
    tags: Optional[List[str]] = []

    @validator('symbol')
    def uppercase_symbol(cls, v):
        return v.upper()

class TradeCreate(TradeBase):
    pass

class TradeResponse(TradeBase):
    id: int
    user_id: str
    pnl: Optional[float]
    created_at: str

# --- Dependency: Authenticated Supabase Client ---

def get_authenticated_client(creds: HTTPAuthorizationCredentials = Depends(security)) -> Client:
    """
    Creates a Supabase client authenticated AS THE USER.
    This ensures all database operations automatically respect RLS policies.
    """
    token = creds.credentials
    # We use the ANON key as the base, but inject the User's JWT
    # Note: Ensure SUPABASE_ANON_KEY is added to your config/env
    client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY) 
    # Ideally use ANON_KEY above, but SERVICE_ROLE works IF we explicitly set the auth header below
    # to 'downgrade' permissions to the user's level. 
    # However, best practice is Client(URL, ANON_KEY, headers={'Authorization': f'Bearer {token}'})
    
    client.postgrest.auth(token)
    return client

# --- Endpoints ---

@router.post("/", response_model=TradeResponse, status_code=status.HTTP_201_CREATED)
def create_trade(
    trade: TradeCreate, 
    supabase: Client = Depends(get_authenticated_client)
):
    """
    Log a new trade via REST. 
    RLS automatically assigns the authenticated user_id.
    """
    # Simple PnL calc logic can remain here or move to frontend/database trigger
    pnl = None
    if trade.exit_price:
        mult = 1 if trade.direction == "Long" else -1
        pnl = (trade.exit_price - trade.entry_price) * trade.quantity * mult - trade.fees

    trade_data = trade.dict()
    trade_data["pnl"] = pnl
    # JSON serialization for datetime
    trade_data["entry_date"] = trade.entry_date.isoformat()
    if trade.exit_date:
        trade_data["exit_date"] = trade.exit_date.isoformat()

    try:
        # Supabase returns a wrapper object; .data contains the rows
        response = supabase.table("trades").insert(trade_data).execute()
        return response.data[0]
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Supabase Error: {str(e)}")

@router.get("/", response_model=List[TradeResponse])
def get_trades(
    skip: int = 0, 
    limit: int = 50, 
    symbol: Optional[str] = None,
    supabase: Client = Depends(get_authenticated_client)
):
    """
    Fetch trades. RLS ensures users only see their own data.
    No manual 'WHERE user_id = ...' needed!
    """
    query = supabase.table("trades").select("*")
    
    if symbol:
        query = query.eq("symbol", symbol.upper())
        
    # Range is 0-indexed, inclusive
    response = query.order("entry_date", desc=True).range(skip, skip + limit - 1).execute()
    return response.data

@router.get("/{trade_id}", response_model=TradeResponse)
def get_trade_detail(
    trade_id: int, 
    supabase: Client = Depends(get_authenticated_client)
):
    """
    Get specific trade.
    If ID exists but belongs to another user, RLS returns empty list -> 404.
    """
    response = supabase.table("trades").select("*").eq("id", trade_id).execute()
    
    if not response.data:
        raise HTTPException(status_code=404, detail="Trade not found")
        
    return response.data[0]

@router.delete("/{trade_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_trade(
    trade_id: int, 
    supabase: Client = Depends(get_authenticated_client)
):
    """
    Delete trade. RLS prevents deleting others' trades.
    """
    response = supabase.table("trades").delete().eq("id", trade_id).execute()
    
    # Supabase delete returns the deleted rows. If empty, nothing was deleted (not found or permission denied)
    if not response.data:
        raise HTTPException(status_code=404, detail="Trade not found or not authorized")
    
    return None