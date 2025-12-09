# backend/app/apis/v1/trades.py
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field, validator
from datetime import datetime
from supabase import create_client, Client

from app.core.config import settings
from app.auth.dependency import get_current_user

router = APIRouter()
security = HTTPBearer()

# --- Pydantic Models (Validation) ---

class TradeBase(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=20)
    
    # ✅ FIX: Added instrument_type field with validation
    instrument_type: str = Field("STOCK", pattern="^(STOCK|CRYPTO|FOREX|FUTURES)$")
    
    direction: str = Field(..., pattern="^(?i)(Long|Short)$")
    status: str = Field("OPEN", pattern="^(?i)(Open|Closed|Pending|Canceled)$")
    
    entry_price: float = Field(..., gt=0)
    exit_price: Optional[float] = Field(None, gt=0)
    
    # ✅ FIX: Changed 'int' to 'float' to match the new 'numeric' DB type
    quantity: float = Field(..., gt=0)
    
    stop_loss: Optional[float] = Field(None, gt=0)
    target: Optional[float] = Field(None, gt=0)
    
    entry_time: datetime
    exit_time: Optional[datetime] = None
    fees: float = Field(0.0, ge=0)
    
    encrypted_notes: Optional[str] = None
    encrypted_screenshots: Optional[List[str]] = []
    strategy_id: Optional[str] = None
    tags: Optional[List[str]] = []

    @validator('symbol')
    def uppercase_symbol(cls, v):
        return v.upper()

    @validator('direction', pre=True)
    def normalize_direction(cls, v):
        return v.upper() 

    @validator('status', pre=True)
    def normalize_status(cls, v):
        return v.upper() 
    
    # ✅ NEW VALIDATOR: Normalize instrument type
    @validator('instrument_type', pre=True)
    def normalize_instrument(cls, v):
        return v.upper()

class TradeCreate(TradeBase):
    pass

class TradeResponse(TradeBase):
    id: str
    user_id: str
    pnl: Optional[float]
    created_at: str

# --- Dependency ---

def get_authenticated_client(creds: HTTPAuthorizationCredentials = Depends(security)) -> Client:
    token = creds.credentials
    client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)
    client.postgrest.auth(token)
    return client

# --- Endpoints ---

@router.post("/", response_model=TradeResponse, status_code=status.HTTP_201_CREATED)
def create_trade(
    trade: TradeCreate, 
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client)
):
    """
    Log a new trade via REST. 
    """
    user_id = current_user["sub"]

    # Simple PnL calc
    pnl = None
    if trade.exit_price:
        mult = 1 if trade.direction == "LONG" else -1
        pnl = (trade.exit_price - trade.entry_price) * trade.quantity * mult - trade.fees

    trade_data = trade.dict()
    trade_data["pnl"] = pnl
    trade_data["user_id"] = user_id
    
    # JSON serialization
    trade_data["entry_time"] = trade.entry_time.isoformat()
    if trade.exit_time:
        trade_data["exit_time"] = trade.exit_time.isoformat()

    try:
        response = supabase.table("trades").insert(trade_data).execute()
        return response.data[0]
    except Exception as e:
        print(f"DB Error: {e}")
        # Note: If a DB error occurs (e.g., constraint violation), the detail should reflect it
        raise HTTPException(status_code=400, detail=f"Database Error: {str(e)}")

@router.get("/", response_model=List[TradeResponse])
def get_trades(
    skip: int = 0, 
    limit: int = 50, 
    symbol: Optional[str] = None,
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client)
):
    query = supabase.table("trades").select("*")
    if symbol:
        query = query.eq("symbol", symbol.upper())
    
    response = query.order("entry_time", desc=True).range(skip, skip + limit - 1).execute()
    return response.data

@router.get("/{trade_id}", response_model=TradeResponse)
def get_trade_detail(
    trade_id: str, 
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client)
):
    response = supabase.table("trades").select("*").eq("id", trade_id).execute()
    if not response.data:
        raise HTTPException(status_code=404, detail="Trade not found")
    return response.data[0]

@router.delete("/{trade_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_trade(
    trade_id: str, 
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client)
):
    response = supabase.table("trades").delete().eq("id", trade_id).execute()
    if not response.data:
        raise HTTPException(status_code=404, detail="Trade not found")
    return None