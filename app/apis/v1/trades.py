# backend/app/apis/v1/trades.py
import logging
from fastapi import APIRouter, Depends, HTTPException, status, Body
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field, validator
from datetime import datetime
from supabase import create_client, Client

from app.core.config import settings
from app.auth.dependency import get_current_user

# --- Configuration & Logging ---
router = APIRouter()
security = HTTPBearer()
logger = logging.getLogger(__name__)

# --- Pydantic Models (Validation) ---

class TradeBase(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=20)
    instrument_type: str = Field("STOCK", pattern="^(STOCK|CRYPTO|FOREX|FUTURES)$")
    direction: str = Field(..., pattern="^(?i)(Long|Short)$")
    status: str = Field("OPEN", pattern="^(?i)(Open|Closed|Pending|Canceled)$")
    entry_price: float = Field(..., gt=0)
    exit_price: Optional[float] = Field(None, gt=0)
    quantity: float = Field(..., gt=0)
    stop_loss: Optional[float] = Field(None, gt=0)
    target: Optional[float] = Field(None, gt=0)
    entry_time: datetime
    exit_time: Optional[datetime] = None
    fees: float = Field(0.0, ge=0)
    encrypted_notes: Optional[str] = None
    notes: Optional[str] = None # Alias for encrypted_notes in payload
    encrypted_screenshots: Optional[List[str]] = []
    strategy_id: Optional[str] = None
    tags: Optional[List[str]] = []

    @validator('symbol')
    def uppercase_symbol(cls, v):
        return v.upper()

    @validator('direction', pre=True)
    def normalize_direction(cls, v):
        return v.title() 

    @validator('status', pre=True)
    def normalize_status(cls, v):
        return v.upper() 
    
    @validator('instrument_type', pre=True)
    def normalize_instrument(cls, v):
        return v.upper()

class TradeCreate(TradeBase):
    pass

class TradeUpdate(BaseModel):
    """
    Partial update model. All fields are optional.
    """
    symbol: Optional[str] = None
    instrument_type: Optional[str] = None
    direction: Optional[str] = None
    status: Optional[str] = None
    entry_price: Optional[float] = None
    exit_price: Optional[float] = None
    quantity: Optional[float] = None
    stop_loss: Optional[float] = None
    target: Optional[float] = None
    entry_time: Optional[datetime] = None
    exit_time: Optional[datetime] = None
    fees: Optional[float] = None
    notes: Optional[str] = None 
    strategy_id: Optional[str] = None

    @validator('symbol')
    def uppercase_symbol(cls, v):
        return v.upper() if v else None

    @validator('direction', pre=True)
    def normalize_direction(cls, v):
        return v.title() if v else None

class TradeResponse(TradeBase):
    id: str
    user_id: str
    pnl: Optional[float]
    created_at: str
    # Map 'notes' back to frontend if needed, usually just sent as encrypted_notes
    notes: Optional[str] = Field(None, alias="encrypted_notes")

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
    user_id = current_user["sub"]
    logger.info(f"User {user_id} creating trade for {trade.symbol}")

    pnl = None
    if trade.exit_price and trade.exit_price > 0:
        mult = 1 if trade.direction == "Long" else -1
        pnl = (float(trade.exit_price) - float(trade.entry_price)) * float(trade.quantity) * mult - float(trade.fees)

    trade_data = trade.dict(exclude={"notes"}) # Exclude alias
    if trade.notes: trade_data["encrypted_notes"] = trade.notes
    
    trade_data["pnl"] = pnl
    trade_data["user_id"] = user_id
    trade_data["entry_time"] = trade.entry_time.isoformat()
    if trade.exit_time:
        trade_data["exit_time"] = trade.exit_time.isoformat()

    try:
        response = supabase.table("trades").insert(trade_data).execute()
        if not response.data:
            raise HTTPException(status_code=500, detail="Failed to create trade record")
        return response.data[0]
    except Exception as e:
        logger.error(f"DB Error creation: {str(e)}")
        raise HTTPException(status_code=400, detail=f"Database Error: {str(e)}")

@router.put("/{trade_id}", response_model=TradeResponse)
def update_trade(
    trade_id: str,
    updates: TradeUpdate,
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client)
):
    """
    Update an existing trade. 
    Smartly recalculates PnL if price/quantity/exit fields change.
    """
    user_id = current_user["sub"]
    
    try:
        # 1. Fetch Existing Trade (Secure Check)
        existing_res = supabase.table("trades").select("*").eq("id", trade_id).eq("user_id", user_id).execute()
        if not existing_res.data:
            raise HTTPException(status_code=404, detail="Trade not found or access denied")
        
        existing_trade = existing_res.data[0]
        
        # 2. Prepare Update Data
        # Filter out None values to only update what was sent
        update_data = {k: v for k, v in updates.dict(exclude={"notes"}).items() if v is not None}
        if updates.notes is not None:
             update_data["encrypted_notes"] = updates.notes

        # 3. Handle Dates Serialization
        if "entry_time" in update_data:
             update_data["entry_time"] = update_data["entry_time"].isoformat()
        if "exit_time" in update_data:
             update_data["exit_time"] = update_data["exit_time"].isoformat()

        # 4. Smart PnL Recalculation
        # We merge existing data with updates to get the "full picture" for calculation
        merged = {**existing_trade, **update_data}
        
        should_recalc = any(k in update_data for k in ["entry_price", "exit_price", "quantity", "direction", "fees"])
        
        if should_recalc:
            # Check if we have enough info to calc PnL (needs exit price)
            exit_p = float(merged.get("exit_price") or 0)
            if exit_p > 0:
                entry_p = float(merged.get("entry_price") or 0)
                qty = float(merged.get("quantity") or 0)
                fees = float(merged.get("fees") or 0)
                direction = merged.get("direction", "Long")
                
                mult = 1 if direction == "Long" else -1
                new_pnl = (exit_p - entry_p) * qty * mult - fees
                
                update_data["pnl"] = new_pnl
                
                # Auto-update status if not explicitly sent
                if "status" not in update_data:
                     update_data["status"] = "CLOSED"
            else:
                # If exit price removed or 0, clear PnL
                update_data["pnl"] = None
                if "status" not in update_data:
                     update_data["status"] = "OPEN"

        # 5. Execute Update
        response = supabase.table("trades").update(update_data).eq("id", trade_id).execute()
        
        if not response.data:
             raise HTTPException(status_code=500, detail="Update failed")
             
        logger.info(f"Trade {trade_id} updated by {user_id}")
        return response.data[0]

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating trade {trade_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Update failed: {str(e)}")

@router.get("/", response_model=List[TradeResponse])
def get_trades(
    skip: int = 0, 
    limit: int = 50, 
    symbol: Optional[str] = None,
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client)
):
    try:
        query = supabase.table("trades").select("*")
        if symbol:
            query = query.eq("symbol", symbol.upper())
        response = query.order("entry_time", desc=True).range(skip, skip + limit - 1).execute()
        return response.data
    except Exception as e:
        logger.error(f"Error fetching trades: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch trades")

@router.delete("/{trade_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_trade(
    trade_id: str, 
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client)
):
    try:
        response = supabase.table("trades").delete().eq("id", trade_id).execute()
        if not response.data:
            raise HTTPException(status_code=404, detail="Trade not found")
        return None
    except Exception as e:
        logger.error(f"Error deleting trade: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete trade")