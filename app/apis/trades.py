# backend/app/apis/trades.py

from fastapi import APIRouter, Depends, HTTPException, status
from loguru import logger
from supabase import Client
from typing import List
from uuid import UUID

from app.auth.dependency import (
    AuthenticatedUser, 
    DBClient, 
    requires_plan, 
    check_ai_consent
)
from app.libs.data_models import (
    TradeCreate, 
    TradeOut, 
    StrategyCreate, 
    StrategyOut
)
from app.libs.crypto_utils import encrypt_data, decrypt_data
from app.libs.task_queue import enqueue_task, _invalidate_edge_cache

router = APIRouter()

# --- HELPER FUNCTIONS ---

def _encrypt_trade_notes(trade_data: TradeCreate):
    """Encrypts the raw_notes field before saving to the database."""
    if trade_data.raw_notes:
        try:
            encrypted = encrypt_data(trade_data.raw_notes)
            # Create a dictionary suitable for insertion
            data_to_insert = trade_data.model_dump(exclude={"raw_notes"}, exclude_none=True)
            data_to_insert['encrypted_notes'] = encrypted
            return data_to_insert
        except Exception as e:
            logger.error(f"ENCRYPTION_FAIL: Could not encrypt trade notes: {e}")
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to secure trade notes.")
    
    # If no notes, just dump the rest of the model
    return trade_data.model_dump(exclude_none=True)

def _decrypt_trade_response(trade_data: dict) -> TradeOut:
    """Decrypts the encrypted_notes field for API output and returns the Pydantic model."""
    if trade_data.get('encrypted_notes'):
        # Add the decrypted content to the 'raw_notes' field
        trade_data['raw_notes'] = decrypt_data(trade_data['encrypted_notes'])
    
    return TradeOut.model_validate(trade_data)

# --- STRATEGY ENDPOINTS ---

@router.post("/strategies", response_model=StrategyOut, status_code=status.HTTP_201_CREATED, 
             summary="Create a new trading strategy")
async def create_strategy(
    strategy: StrategyCreate,
    user: AuthenticatedUser,
    db: Client = Depends(DBClient),
    _ = Depends(requires_plan("STRATEGY_CREATE")) 
):
    """
    Creates a new user-specific trading strategy template (Playbook).
    (RLS & Modular)
    """
    data_to_insert = strategy.model_dump(exclude_none=True)
    data_to_insert['user_id'] = user.user_id

    try:
        response = db.table('strategies').insert(data_to_insert).execute()
        
        # Invalidate the cache for the user's strategy list (Efficiency)
        await _invalidate_edge_cache(payload={"cache_path": f"/v1/trades/strategies/list/{user.user_id}"})
        
        logger.success(f"STRATEGY_CREATE: User {user.user_id} created new strategy '{strategy.name}'.")
        return StrategyOut.model_validate(response.data[0])
    except Exception as e:
        logger.error(f"DB_ERROR: Failed to create strategy for user {user.user_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to save strategy.")


@router.get("/strategies", response_model=List[StrategyOut], summary="List all user strategies")
async def list_strategies(
    user: AuthenticatedUser,
    db: Client = Depends(DBClient)
):
    """
    Retrieves all strategies belonging to the authenticated user.
    (RLS Enforced, Caching Supported)
    """
    try:
        response = db.table('strategies').select('*').eq('user_id', user.user_id).order('created_at', desc=True).execute()
        
        # NOTE: Read endpoints should set Cache-Control headers, but we rely on Vercel/Render settings
        # to apply a max-age cache policy for global speed.
        
        return [StrategyOut.model_validate(item) for item in response.data]
    except Exception as e:
        logger.error(f"DB_ERROR: Failed to list strategies for user {user.user_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to retrieve strategies.")


# (TODO: Add GET/{id}, PUT/{id}, DELETE/{id} for strategies)

# --- TRADE ENDPOINTS ---

@router.post("/trades", response_model=TradeOut, status_code=status.HTTP_201_CREATED, 
             summary="Log a new manual trade entry")
async def create_trade(
    trade_data: TradeCreate,
    user: AuthenticatedUser,
    db: Client = Depends(DBClient),
    _ = Depends(requires_plan("CREATE_TRADE_MANUAL")) 
):
    """
    Logs a new trade. Encrypts sensitive notes before saving. Triggers AI analysis asynchronously.
    (Security, Privacy, Super Fast)
    """
    # 1. Encrypt sensitive data (Privacy Policy 1.B)
    data_to_insert = _encrypt_trade_notes(trade_data)
    data_to_insert['user_id'] = user.user_id

    try:
        # 2. Insert trade into the RLS-enforced table
        response = db.table('trades').insert(data_to_insert).execute()
        new_trade = response.data[0]
        
        # 3. Trigger Asynchronous AI Analysis (Robustness/Efficiency)
        # This offloads processing, keeping the API fast.
        await enqueue_task(
            task_name="trade_analysis", 
            payload={
                "trade_id": str(new_trade['id']), 
                "user_id": str(user.user_id),
                "encrypted_notes": new_trade.get('encrypted_notes')
            }
        )
        
        # 4. Invalidate cache for the trades list and dashboard (Edge-First Architecture)
        await _invalidate_edge_cache(payload={"cache_path": f"/v1/trades/list/{user.user_id}"})
        await _invalidate_edge_cache(payload={"cache_path": f"/v1/analytics/kpis/{user.user_id}"})
        
        logger.success(f"TRADE_CREATE: User {user.user_id} logged trade {new_trade['id']}. AI job queued.")
        
        # 5. Return the trade with notes decrypted for immediate frontend display
        return _decrypt_trade_response(new_trade)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"DB_ERROR: Failed to create trade for user {user.user_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to log trade entry.")


@router.get("/trades", response_model=List[TradeOut], summary="Retrieve a list of all user trades")
async def list_trades(
    user: AuthenticatedUser,
    db: Client = Depends(DBClient)
):
    """
    Retrieves a paginated list of all trades for the user.
    (RLS Enforced, Caching Supported)
    """
    try:
        # Fetch up to 50 trades, RLS ensures we only see our own.
        response = db.table('trades').select('*').eq('user_id', user.user_id).order('entry_time', desc=True).limit(50).execute()
        
        # Decrypt notes before returning to client (Privacy/Security boundary)
        trades_list = [_decrypt_trade_response(item) for item in response.data]
        
        return trades_list

    except Exception as e:
        logger.error(f"DB_ERROR: Failed to list trades for user {user.user_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to retrieve trade list.")

# (TODO: Add GET/{id}, PUT/{id}, DELETE/{id} for trades)