import logging
import json
import asyncio
import io
import csv
from uuid import uuid4
from datetime import datetime
from typing import List, Dict, Any, AsyncGenerator, Optional

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    status,
    UploadFile,
    File,
    Request,
)
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool
from supabase import create_client, Client

from app.core.config import settings
from app.core.database import db
from app.auth.dependency import get_current_user
from app.lib.data_sanitizer import sanitizer
from app.services.quota_manager import QuotaManager
from app.lib.encryption import crypto

# Import schemas
from app.schemas.trade_schemas import (
    TradeCreate,
    TradeUpdate,
    TradeResponse,
)

logger = logging.getLogger("tradeomen.trades")
router = APIRouter()

# Initialize Supabase
supabase_storage: Client = create_client(
    settings.SUPABASE_URL, 
    settings.SUPABASE_SERVICE_ROLE_KEY
)


# ---------------------------------------------------------------------
# Services (Separation of Concerns & Helper Logic)
# ---------------------------------------------------------------------

class TradeService:
    @staticmethod
    def serialize_row(row: Any) -> Dict[str, Any]:
        """
        Converts DB row to API response format.
        Handles robust type conversion and JSON parsing.
        """
        if not row:
            return {}
        
        d = dict(row)
        
        # 1. Type Conversion
        for k, v in d.items():
            if isinstance(v, datetime):
                d[k] = v.isoformat()
            elif hasattr(v, 'hex'):  # Handle UUIDs
                d[k] = str(v)

        # 2. JSON & Metadata Handling
        if "metadata" in d:
            d["metadata"] = TradeService._parse_json(d["metadata"], default_value={})

        # 3. Field Mapping (Plain Text Notes)
        if "encrypted_notes" in d:
            d["notes"] = d.pop("encrypted_notes")
        
        # 4. Screenshots (Handle JSON Array of Encrypted Strings)
        raw_screenshots = d.get("encrypted_screenshots") or d.get("screenshots")
        
        # Default to list [] to prevent "Input should be a valid list" error
        parsed_screenshots = TradeService._parse_json(raw_screenshots, default_value=[])
        
        # Double check it's a list (in case DB has a JSON string representing a dict)
        if not isinstance(parsed_screenshots, list):
            parsed_screenshots = []
            
        d["screenshots"] = parsed_screenshots
        
        # Initialize signed field for Schema compatibility
        d["screenshots_signed"] = None
        
        return d

    @staticmethod
    def _parse_json(val: Any, default_value: Any) -> Any:
        """
        Parses JSON safely. Returns default_value if parsing fails or input is None.
        """
        if isinstance(val, str):
            try: return json.loads(val)
            except ValueError: return default_value
        return val if val is not None else default_value

    @staticmethod
    def _get_user_id(user: Dict[str, Any]) -> str:
        return user["user_id"]


class ScreenshotService:
    @staticmethod
    def is_safe_file(file: UploadFile) -> bool:
        if file.content_type not in settings.ALLOWED_IMAGE_TYPES:
            return False
        
        # Robust extension check
        filename = file.filename.lower()
        allowed_exts = {".png", ".jpg", ".jpeg", ".webp"}
        return any(filename.endswith(ext) for ext in allowed_exts)

    @staticmethod
    async def sign_urls_async(paths: List[str]) -> List[Dict[str, str]]:
        """
        Iterates over a list of strings (encrypted or plain).
        Decrypts 'gAAAA...' tokens to get the real path for signing.
        Uses AsyncIO to prevent blocking the event loop.
        """
        if not paths:
            return []

        bucket = getattr(settings, "SCREENSHOT_BUCKET", "trade_screenshots")

        def _sign(p: str):
            try:
                # Fernet tokens start with gAAAA; decrypt if needed
                real_path = crypto.decrypt(p) if p.startswith("gAAAA") else p
                
                res = supabase_storage.storage.from_(bucket).create_signed_url(real_path, 3600)
                
                # Handle Supabase client response variations
                url = res.get("signedURL") if isinstance(res, dict) else getattr(res, "signed_url", "")
                
                # Return the original (encrypted) path so frontend keeps the reference
                return {"path": p, "url": url}
            except Exception as e:
                logger.error(f"Signing failed for {p}: {e}")
                return {"path": p, "url": ""}

        # Run in threadpool to prevent blocking async loop with synchronous Supabase calls
        tasks = [run_in_threadpool(_sign, path) for path in paths]
        return await asyncio.gather(*tasks)


async def _check_trade_quota(user_id: str, plan_tier: str):
    # Robust: Rely on config limits. If limit is None, it is unlimited.
    limits = settings.get_plan_limits(plan_tier)
    max_trades = limits.get("max_trades_per_month")
    
    if max_trades is None: return

    # Optimized count query
    query = """
        SELECT COUNT(*) FROM trades 
        WHERE user_id = $1 
        AND created_at >= date_trunc('month', NOW())
    """
    count = await db.fetch_val(query, user_id)
    if count >= max_trades:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, f"Monthly trade limit reached ({max_trades})")


# ---------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------

@router.post("/", response_model=TradeResponse, status_code=201)
async def create_trade(
    trade: TradeCreate,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Create a new trade. Handles encryption, PnL calculation, and Quotas.
    """
    user_id = TradeService._get_user_id(current_user)
    
    # 1. Enforce Quotas & Feature Flags
    await _check_trade_quota(user_id, current_user.get("plan_tier", "FREE"))

    if trade.screenshots:
        await QuotaManager.require_feature(current_user, "allow_screenshots")

    if trade.tags:
        await QuotaManager.require_feature(current_user, "allow_tags")

    # 2. Notes: Store as PLAIN TEXT (Sanitized only)
    notes = sanitizer.sanitize(trade.notes) if trade.notes else None
    
    # 3. Screenshots: Encrypt path strings
    screenshots_data = [
        s if s.startswith("gAAAA") else crypto.encrypt(s) 
        for s in (trade.screenshots or [])
    ]
    
    # 4. PnL Calculation
    pnl = trade.pnl
    if pnl is None and trade.exit_price and trade.quantity:
        multiplier = 1 if trade.direction == "LONG" else -1
        diff = (trade.exit_price - trade.entry_price)
        pnl = (diff * trade.quantity * multiplier) - (trade.fees or 0)

    # 5. Insert (Using positional args $1, $2...)
    # ✅ Fix: Use $17 (text) for screenshots instead of $17::jsonb since the column is TEXT
    query = """
        INSERT INTO trades (
            user_id, symbol, instrument_type, direction, status,
            entry_price, quantity, entry_time, exit_price, exit_time,
            stop_loss, target, fees, pnl,
            encrypted_notes, tags, encrypted_screenshots, strategy_id, metadata
        ) VALUES (
            $1, $2, $3, $4, $5,
            $6, $7, $8, $9, $10,
            $11, $12, $13, $14,
            $15, $16, $17, $18, $19::jsonb
        )
        RETURNING *
    """
    
    try:
        row = await db.fetch_one(
            query,
            user_id, trade.symbol, trade.instrument_type, trade.direction, trade.status,
            trade.entry_price, trade.quantity, trade.entry_time, trade.exit_price, trade.exit_time,
            trade.stop_loss, trade.target, trade.fees, pnl,
            notes, trade.tags, json.dumps(screenshots_data), trade.strategy_id, json.dumps(trade.metadata or {})
        )
    except Exception as e:
        logger.error(f"Trade creation failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to save trade")

    return TradeService.serialize_row(row)


@router.get("/{trade_id}", response_model=TradeResponse)
async def get_trade(
    trade_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    user_id = TradeService._get_user_id(current_user)
    
    query = """
        SELECT t.*, s.name as strategy_name, s.emoji as strategy_emoji
        FROM trades t
        LEFT JOIN strategies s ON t.strategy_id = s.id
        WHERE t.id = $1 AND t.user_id = $2
    """
    row = await db.fetch_one(query, trade_id, user_id)
    
    if not row: 
        raise HTTPException(404, "Trade not found")
        
    data = TradeService.serialize_row(row)
    
    # Non-blocking async signing of screenshots
    raw_screenshots = data.get("screenshots", [])
    data["screenshots_signed"] = await ScreenshotService.sign_urls_async(raw_screenshots)
    
    if data.get("strategy_name"):
        data["strategies"] = {"name": data.pop("strategy_name"), "emoji": data.pop("strategy_emoji")}
    else:
        data["strategies"] = None

    return data


@router.get("/{trade_id}/screenshots", response_model=List[Dict[str, str]])
async def get_trade_screenshots(
    trade_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Get signed URLs for screenshots associated with a specific trade.
    """
    user_id = TradeService._get_user_id(current_user)
    
    query = "SELECT encrypted_screenshots FROM trades WHERE id = $1 AND user_id = $2"
    row = await db.fetch_one(query, trade_id, user_id)
    
    if not row:
        raise HTTPException(404, "Trade not found")
        
    raw_data = row["encrypted_screenshots"]
    
    # Ensure default is a list for the API response
    paths = TradeService._parse_json(raw_data, default_value=[])
    if not isinstance(paths, list):
        paths = []
    
    return await ScreenshotService.sign_urls_async(paths)


@router.put("/{trade_id}", response_model=TradeResponse)
async def update_trade(
    trade_id: str,
    payload: TradeUpdate,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    user_id = TradeService._get_user_id(current_user)
    update_data = payload.model_dump(exclude_unset=True)
    
    if not update_data: 
        raise HTTPException(400, "No fields to update")

    # Enforce Feature Flags during Update
    if "screenshots" in update_data and update_data["screenshots"]:
        await QuotaManager.require_feature(current_user, "allow_screenshots")

    if "tags" in update_data and update_data["tags"]:
        await QuotaManager.require_feature(current_user, "allow_tags")

    # Handle encryption/sanitization
    if "notes" in update_data:
        # PLAIN TEXT only
        update_data["encrypted_notes"] = sanitizer.sanitize(update_data.pop("notes"))
    
    if "screenshots" in update_data:
        raw_list = update_data.pop("screenshots")
        enc_list = [
            s if s.startswith("gAAAA") else crypto.encrypt(s) 
            for s in raw_list
        ]
        update_data["encrypted_screenshots"] = json.dumps(enc_list)

    if "metadata" in update_data:
        update_data["metadata"] = json.dumps(update_data["metadata"])

    # Dynamic SQL generation using positional args
    set_clauses = []
    values = []
    idx = 1
    
    for key, val in update_data.items():
        if key in ["metadata"]:
             set_clauses.append(f"{key} = ${idx}::jsonb")
             values.append(val)
        elif key in ["encrypted_screenshots"]:
             # Explicitly keep as text since column is text
             set_clauses.append(f"{key} = ${idx}")
             values.append(val)
        else:
            set_clauses.append(f"{key} = ${idx}")
            values.append(val)
        idx += 1
    
    values.append(trade_id)
    values.append(user_id)
    
    query = f"""
        UPDATE trades
        SET {", ".join(set_clauses)}
        WHERE id = ${idx} AND user_id = ${idx + 1}
        RETURNING *
    """
    
    row = await db.fetch_one(query, *values)
    if not row: 
        raise HTTPException(404, "Trade not found")
        
    return TradeService.serialize_row(row)


@router.delete("/{trade_id}", status_code=204)
async def delete_trade(
    trade_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    user_id = TradeService._get_user_id(current_user)
    query = "DELETE FROM trades WHERE id = $1 AND user_id = $2 RETURNING id"
    row = await db.fetch_one(query, trade_id, user_id)
    
    if not row:
        exists = await db.fetch_val("SELECT id FROM trades WHERE id = $1", trade_id)
        if exists: raise HTTPException(403, "Forbidden")
        raise HTTPException(404, "Trade not found")


@router.get("/export/csv")
async def export_trades(current_user: Dict[str, Any] = Depends(get_current_user)):
    """
    PRO Feature: Export trades to CSV.
    Uses Batched Streaming to prevent memory overflow (OOM) on large datasets.
    """
    # Enforce Feature Flag
    await QuotaManager.require_feature(current_user, "allow_export_csv")

    user_id = TradeService._get_user_id(current_user)

    async def iter_csv_generator() -> AsyncGenerator[str, None]:
        # Yield Header
        yield "Symbol,Type,Side,Entry Price,Exit Price,Qty,PnL,Date\n"
        
        # Cursor-based pagination or Chunked fetch for memory efficiency
        # Using Limit/Offset batching to be safe with basic DB drivers
        limit = 1000
        offset = 0
        
        while True:
            query = """
                SELECT symbol, instrument_type, direction, entry_price, 
                       exit_price, quantity, pnl, entry_time
                FROM trades 
                WHERE user_id = $1 
                ORDER BY entry_time DESC
                LIMIT $2 OFFSET $3
            """
            rows = await db.fetch_all(query, user_id, limit, offset)
            
            if not rows:
                break
                
            for row in rows:
                # Manual CSV formatting is faster than csv.writer for streaming
                date_str = row["entry_time"].isoformat() if row["entry_time"] else ""
                line = (
                    f"{row['symbol']},{row['instrument_type']},{row['direction']},"
                    f"{row['entry_price']},{row['exit_price']},{row['quantity']},"
                    f"{row['pnl']},{date_str}\n"
                )
                yield line
            
            offset += limit
            if len(rows) < limit:
                break

    return StreamingResponse(
        iter_csv_generator(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=trades_export.csv"}
    )


@router.post("/uploads/trade-screenshots", status_code=201)
async def upload_trade_screenshot(
    request: Request,
    files: List[UploadFile] = File(...),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    user_id = TradeService._get_user_id(current_user)
    
    # Await the async quota check
    await QuotaManager.require_feature(current_user, "allow_screenshots")

    uploaded_results = []
    new_paths_encrypted = []
    bucket_name = getattr(settings, "SCREENSHOT_BUCKET", "trade_screenshots")

    for file in files:
        if not ScreenshotService.is_safe_file(file):
            continue
            
        ext = file.filename.split(".")[-1].lower()
        filename = f"{uuid4().hex}.{ext}"
        path = f"{user_id}/{filename}"
        
        contents = await file.read()
        
        try:
            # Upload to storage (Blocking I/O offloaded)
            await run_in_threadpool(
                lambda: supabase_storage.storage.from_(bucket_name).upload(
                    path, contents, {"content-type": file.content_type}
                )
            )
            
            # Encrypt path immediately (Fernet gAAAA...)
            enc_path = crypto.encrypt(path)
            new_paths_encrypted.append(enc_path)
            
            # Generate signed URL
            signed_url_res = await run_in_threadpool(
                lambda: supabase_storage.storage.from_(bucket_name).create_signed_url(path, 3600)
            )
            
            url = signed_url_res.get("signedURL") if isinstance(signed_url_res, dict) else getattr(signed_url_res, "signed_url", "")
            
            uploaded_results.append({
                "filename": file.filename, 
                "path": enc_path,
                "url": url
            })
            
        except Exception as e:
            logger.error(f"Upload failed: {e}")

    if not uploaded_results:
        raise HTTPException(400, "No valid files uploaded")

    # Atomic Update Logic
    trade_id = request.query_params.get("trade_id")
    uploaded_to_trade = False

    if trade_id and new_paths_encrypted:
        try:
            # Atomic Append using Postgres JSONB operator (||)
            # ✅ FIX: Explicit cast from TEXT -> JSONB, concat, then cast back to TEXT
            new_json_fragment = json.dumps(new_paths_encrypted)
            
            update_query = """
                UPDATE trades 
                SET encrypted_screenshots = 
                    (COALESCE(encrypted_screenshots::jsonb, '[]'::jsonb) || $1::jsonb)::text
                WHERE id = $2 AND user_id = $3
            """
            
            await db.execute(update_query, new_json_fragment, trade_id, user_id)
            uploaded_to_trade = True
            
        except Exception as e:
            logger.error(f"Failed to link screenshots atomically: {e}")

    return {
        "files": uploaded_results, 
        "uploaded_to_trade": uploaded_to_trade, 
        "count": len(uploaded_results)
    }