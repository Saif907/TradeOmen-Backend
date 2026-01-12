import logging
import json
import uuid
from datetime import datetime
from typing import List, Dict, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.config import settings
from app.core.database import db
from app.auth.dependency import get_current_user

# Ensure these schemas exist in app/schemas/strategy_schemas.py
from app.schemas.strategy_schemas import (
    StrategyCreate,
    StrategyUpdate,
    StrategyResponse
)

logger = logging.getLogger("tradeomen.strategies")
router = APIRouter()


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def _get_user_id(user: Dict[str, Any]) -> str:
    return user["user_id"]


def _serialize_row(row: Any) -> Dict[str, Any]:
    """
    Converts database types (UUID, datetime, arrays) into Pydantic-friendly formats.
    """
    if not row:
        return {}
    
    d = dict(row)
    
    for k, v in d.items():
        if isinstance(v, uuid.UUID):
            d[k] = str(v)
        elif isinstance(v, datetime):
            d[k] = v.isoformat()
            
    # Handle JSON fields
    if "rules" in d:
        if isinstance(d["rules"], str):
            try:
                d["rules"] = json.loads(d["rules"])
            except:
                d["rules"] = {}
        elif d["rules"] is None:
            d["rules"] = {}

    # Handle Arrays (instrument_types is typically TEXT[] in Postgres)
    if "instrument_types" in d and d["instrument_types"] is None:
        d["instrument_types"] = []

    return d


async def _check_strategy_quota(user_id: str, plan_tier: str):
    """
    Enforce strategy count limits using fast SQL.
    """
    if plan_tier == "PREMIUM":
        return

    limits = settings.get_plan_limits(plan_tier)
    # "max_strategies" key comes from config.py PLAN_DEFINITIONS
    max_count = limits.get("max_strategies")

    if max_count is None: # Unlimited
        return

    query = "SELECT COUNT(*) FROM strategies WHERE user_id = $1"
    count = await db.fetch_val(query, user_id)
    
    if count >= max_count:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Strategy limit reached ({max_count}) for {plan_tier} plan."
        )


# ---------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------

@router.get("/", response_model=List[StrategyResponse])
async def get_strategies(
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    user_id = _get_user_id(current_user)
    
    query = """
        SELECT * FROM strategies 
        WHERE user_id = $1 
        ORDER BY created_at DESC
    """
    rows = await db.fetch_all(query, user_id)
    
    return [_serialize_row(row) for row in rows]


@router.get("/{strategy_id}", response_model=StrategyResponse)
async def get_strategy(
    strategy_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    user_id = _get_user_id(current_user)
    
    query = "SELECT * FROM strategies WHERE id = $1 AND user_id = $2"
    row = await db.fetch_one(query, strategy_id, user_id)
    
    if not row:
        raise HTTPException(status_code=404, detail="Strategy not found")
        
    return _serialize_row(row)


@router.post("/", response_model=StrategyResponse, status_code=status.HTTP_201_CREATED)
async def create_strategy(
    strategy: StrategyCreate,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    user_id = _get_user_id(current_user)
    
    # 1. Enforce Quota
    await _check_strategy_quota(user_id, current_user.get("plan_tier", "FREE"))

    # 2. Prepare Data
    data = strategy.model_dump()
    
    # Serialize rules to JSON string for jsonb column
    rules_json = json.dumps(data.get("rules", {}))
    
    # Note: 'instrument_types' is passed as a list; asyncpg handles TEXT[] conversion automatically.
    
    query = """
        INSERT INTO strategies (
            user_id, name, description, emoji, color_hex, style, 
            instrument_types, rules, track_missed_trades, created_at
        ) VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, NOW()
        )
        RETURNING *
    """
    
    try:
        row = await db.fetch_one(
            query,
            user_id,
            data["name"],
            data.get("description"),
            data.get("emoji", "♟️"),
            data.get("color_hex", "#FFFFFF"),
            data.get("style"),
            data.get("instrument_types", []),
            rules_json,
            data.get("track_missed_trades", False)
        )
    except Exception as e:
        logger.error(f"Strategy creation failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to create strategy")

    return _serialize_row(row)


@router.patch("/{strategy_id}", response_model=StrategyResponse)
async def update_strategy(
    strategy_id: str,
    strategy: StrategyUpdate,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    user_id = _get_user_id(current_user)
    
    # 1. Filter update data
    update_data = strategy.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(status_code=400, detail="No fields to update")

    # 2. Build Dynamic Query
    set_clauses = []
    values = []
    idx = 1
    
    for key, val in update_data.items():
        if key == "rules":
            set_clauses.append(f"{key} = ${idx}::jsonb")
            values.append(json.dumps(val))
        else:
            set_clauses.append(f"{key} = ${idx}")
            values.append(val)
        idx += 1
    
    # Add ID parameters for WHERE clause
    values.append(strategy_id)
    values.append(user_id)
    
    query = f"""
        UPDATE strategies
        SET {", ".join(set_clauses)}
        WHERE id = ${idx} AND user_id = ${idx + 1}
        RETURNING *
    """
    
    row = await db.fetch_one(query, *values)
    if not row:
        raise HTTPException(status_code=404, detail="Strategy not found")
        
    return _serialize_row(row)


@router.delete("/{strategy_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_strategy(
    strategy_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    user_id = _get_user_id(current_user)
    
    # Using RETURNING id allows us to verify if anything was actually deleted
    query = "DELETE FROM strategies WHERE id = $1 AND user_id = $2 RETURNING id"
    row = await db.fetch_one(query, strategy_id, user_id)
    
    if not row:
        # Optional: Check if it existed but belonged to someone else for 403 vs 404
        raise HTTPException(status_code=404, detail="Strategy not found")