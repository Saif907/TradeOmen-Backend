import logging
import json
import io
import csv
import uuid
from uuid import uuid4
from datetime import datetime
from typing import List, Optional, Dict, Any

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    status,
    Query,
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
from app.lib.encryption import crypto  # ✅ Added for screenshot decryption

# Import schemas
from app.schemas.trade_schemas import (
    TradeCreate,
    TradeUpdate,
    TradeResponse,
)

logger = logging.getLogger("tradeomen.trades")
router = APIRouter()

# Admin client (ONLY used for Storage Buckets operations)
supabase_storage: Client = create_client(
    settings.SUPABASE_URL, 
    settings.SUPABASE_SERVICE_ROLE_KEY
)


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def _get_user_id(user: Dict[str, Any]) -> str:
    return user["user_id"]


def _serialize_row(row: Any) -> Dict[str, Any]:
    """
    Robustly converts database types to JSON-compatible types for Pydantic.
    Maps DB columns (encrypted_*) to API fields for detailed views.
    """
    if not row:
        return {}
    
    d = dict(row)
    
    for k, v in d.items():
        if isinstance(v, uuid.UUID):
            d[k] = str(v)
        elif isinstance(v, datetime):
            d[k] = v.isoformat()
            
    # Handle JSON Fields
    if "metadata" in d:
        val = d["metadata"]
        if isinstance(val, str):
            try: d["metadata"] = json.loads(val)
            except: d["metadata"] = {}
        elif val is None: d["metadata"] = {}

    # ✅ Handle Screenshots: Map 'encrypted_screenshots' -> 'screenshots'
    # The DB stores a JSON array of ENCRYPTED strings. 
    screenshot_source = d.get("encrypted_screenshots") or d.get("screenshots")
    
    if screenshot_source:
        if isinstance(screenshot_source, str):
            try:
                d["screenshots"] = json.loads(screenshot_source)
            except:
                d["screenshots"] = [screenshot_source]
        elif isinstance(screenshot_source, list):
            d["screenshots"] = screenshot_source
        else:
            d["screenshots"] = []
    else:
        d["screenshots"] = []
    
    # ✅ Handle Notes: Map 'encrypted_notes' -> 'notes'
    if "encrypted_notes" in d:
        d["notes"] = d["encrypted_notes"]

    return d


async def _check_trade_quota(user_id: str, plan_tier: str):
    if plan_tier == "PREMIUM": return
    limits = settings.get_plan_limits(plan_tier)
    max_trades = limits.get("max_trades_per_month")
    if max_trades is None: return

    query = """
        SELECT COUNT(*) FROM trades 
        WHERE user_id = $1 AND date_trunc('month', created_at) = date_trunc('month', NOW())
    """
    count = await db.fetch_val(query, user_id)
    if count >= max_trades:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, f"Monthly trade limit reached ({max_trades})")


# ---------------------------------------------------------------------
# Screenshot Service
# ---------------------------------------------------------------------

class ScreenshotService:
    @staticmethod
    def try_decrypt(path: str) -> str:
        """Attempt to decrypt a path. If it fails or isn't encrypted, return as is."""
        try:
            if path.startswith("gAAAA"):
                decrypted = crypto.decrypt(path)
                return decrypted if decrypted else path
            return path
        except Exception:
            return path

    @staticmethod
    def sign_url(path: str) -> str:
        # Decrypt path before asking Supabase for a signed URL
        real_path = ScreenshotService.try_decrypt(path)
        
        try:
            bucket = getattr(settings, "SCREENSHOT_BUCKET", "trade_screenshots")
            
            res = supabase_storage.storage.from_(bucket).create_signed_url(real_path, 3600)
            if isinstance(res, dict):
                return res.get("signedURL") or res.get("signed_url")
            return res 
        except Exception as e:
            logger.warning(f"Failed to sign URL for {real_path}: {e}")
            return ""

    @staticmethod
    def process_paths(paths_json: Any) -> List[Dict[str, str]]:
        if not paths_json: return []
        
        try:
            paths = json.loads(paths_json) if isinstance(paths_json, str) else paths_json
            if not isinstance(paths, list): paths = [str(paths)]
        except:
            paths = [str(paths_json)]

        results = []
        for p in paths:
            if not p or "Decryption Error" in p: continue
            
            # The URL needs the DECRYPTED path
            url = ScreenshotService.sign_url(p)
            
            # We return the original (potentially encrypted) path so updates work correctly
            results.append({"path": p, "url": url})
            
        return results


# ---------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------

# NOTE: GET /trades/ (List) has been REMOVED to enforce frontend "Direct Read".

@router.post("/", response_model=TradeResponse, status_code=201)
async def create_trade(
    trade: TradeCreate,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Create a new trade. Handles encryption, PnL calculation, and Quotas.
    """
    user_id = _get_user_id(current_user)
    await _check_trade_quota(user_id, current_user.get("plan_tier", "FREE"))

    # Notes are Plain Text (based on sample) but mapped to encrypted_notes col
    notes = sanitizer.sanitize(trade.notes) if trade.notes else None
    
    # Screenshots are Encrypted
    screenshots_data = []
    if trade.screenshots:
        for s in trade.screenshots:
            if s.startswith("gAAAA"):
                screenshots_data.append(s)
            else:
                screenshots_data.append(crypto.encrypt(s))
                
    screenshots_json = json.dumps(screenshots_data)

    # Calculate PnL Once
    pnl = trade.pnl
    if pnl is None and trade.exit_price and trade.quantity:
        diff = (trade.exit_price - trade.entry_price)
        pnl = (diff * trade.quantity) if trade.direction == "LONG" else (diff * -1 * trade.quantity)
        pnl -= (trade.fees or 0)

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
            $15, $16, $17::jsonb, $18, $19::jsonb
        )
        RETURNING *
    """
    
    try:
        row = await db.fetch_one(
            query,
            user_id, trade.symbol, trade.instrument_type, trade.direction, trade.status,
            trade.entry_price, trade.quantity, trade.entry_time, trade.exit_price, trade.exit_time,
            trade.stop_loss, trade.target, trade.fees, pnl,
            notes, trade.tags, screenshots_json, trade.strategy_id, json.dumps(trade.metadata or {})
        )
    except Exception as e:
        logger.error(f"Trade creation failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to save trade")

    return _serialize_row(row)


@router.get("/{trade_id}", response_model=TradeResponse)
async def get_trade(
    trade_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Fetch a single trade by ID.
    Used by the frontend to retrieve SENSITIVE data (notes/screenshots)
    that are excluded from the main list query for security.
    """
    user_id = _get_user_id(current_user)
    
    query = """
        SELECT t.*, s.name as strategy_name, s.emoji as strategy_emoji
        FROM trades t
        LEFT JOIN strategies s ON t.strategy_id = s.id
        WHERE t.id = $1 AND t.user_id = $2
    """
    row = await db.fetch_one(query, trade_id, user_id)
    
    if not row: raise HTTPException(404, "Trade not found")
        
    d = _serialize_row(row)
    
    # Generate signed URLs using the decrypted paths
    d["screenshots_signed"] = ScreenshotService.process_paths(d.get("screenshots"))
    
    if d.get("strategy_name"):
        d["strategies"] = {"name": d.pop("strategy_name"), "emoji": d.pop("strategy_emoji")}
    else:
        d["strategies"] = None

    return d


@router.put("/{trade_id}", response_model=TradeResponse)
async def update_trade(
    trade_id: str,
    payload: TradeUpdate,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    user_id = _get_user_id(current_user)
    update_data = payload.model_dump(exclude_unset=True)
    if not update_data: raise HTTPException(400, "No fields to update")

    # Handle encryption for updates
    if "notes" in update_data:
        update_data["encrypted_notes"] = sanitizer.sanitize(update_data.pop("notes"))
    
    if "screenshots" in update_data:
        raw_list = update_data.pop("screenshots")
        enc_list = []
        for s in raw_list:
            if s.startswith("gAAAA"):
                enc_list.append(s)
            else:
                enc_list.append(crypto.encrypt(s))
        update_data["encrypted_screenshots"] = json.dumps(enc_list)

    set_clauses = []
    values = []
    idx = 1
    
    for key, val in update_data.items():
        if key in ["metadata", "encrypted_screenshots"]:
             set_clauses.append(f"{key} = ${idx}::jsonb")
             values.append(json.dumps(val) if not isinstance(val, str) else val)
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
    if not row: raise HTTPException(404, "Trade not found")
        
    return _serialize_row(row)


@router.delete("/{trade_id}", status_code=204)
async def delete_trade(
    trade_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    user_id = _get_user_id(current_user)
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
    Uses backend read to bypass frontend limits and ensure plan compliance.
    """
    user_id = _get_user_id(current_user)
    plan = current_user.get("plan_tier", "FREE")
    
    if plan not in ["PRO", "PREMIUM"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Export is a PRO feature.")

    # Optimized read for CSV
    query = """
        SELECT symbol, instrument_type, direction, entry_price, exit_price, quantity, pnl, entry_time
        FROM trades WHERE user_id = $1 ORDER BY entry_time DESC
    """
    rows = await db.fetch_all(query, user_id)
    
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Symbol", "Type", "Side", "Entry Price", "Exit Price", "Qty", "PnL", "Date"])
    
    for row in rows:
        writer.writerow([
            row["symbol"], row["instrument_type"], row["direction"],
            row["entry_price"], row["exit_price"], row["quantity"],
            row["pnl"], row["entry_time"]
        ])
    
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=trades_export.csv"}
    )


@router.post("/uploads/trade-screenshots", status_code=201)
async def upload_trade_screenshot(
    request: Request,
    files: List[UploadFile] = File(...),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    user_id = _get_user_id(current_user)
    QuotaManager.require_feature(current_user, "allow_screenshots")

    uploaded_results = []
    new_paths = []

    bucket_name = getattr(settings, "SCREENSHOT_BUCKET", "trade_screenshots")

    for file in files:
        if file.content_type not in settings.ALLOWED_IMAGE_TYPES: continue
        contents = await file.read()
        if len(contents) > settings.MAX_UPLOAD_SIZE_BYTES: continue

        ext = file.filename.split(".")[-1] if "." in file.filename else "png"
        filename = f"{uuid4().hex}.{ext}"
        path = f"{user_id}/{filename}"

        try:
            def _upload():
                return supabase_storage.storage.from_(bucket_name).upload(
                    path, contents, {"content-type": file.content_type}
                )
            await run_in_threadpool(_upload)
            
            # Return encrypted path logic handled by frontend, we just return the raw path for now
            # and let the frontend send it back to update_trade which handles encryption.
            signed = ScreenshotService.sign_url(path)
            uploaded_results.append({"filename": file.filename, "path": path, "url": signed})
            new_paths.append(path)
        except Exception as e:
            logger.error(f"Upload failed: {e}")

    if not uploaded_results: raise HTTPException(400, "No valid files uploaded")

    # If trade_id is provided, link immediately (Atomic update)
    trade_id = request.query_params.get("trade_id")
    uploaded_to_trade = False

    if trade_id and new_paths:
        try:
            existing = await db.fetch_one(
                "SELECT encrypted_screenshots, user_id FROM trades WHERE id = $1", 
                trade_id
            )
            
            if existing and str(existing["user_id"]) == str(user_id):
                current_paths = []
                if existing["encrypted_screenshots"]:
                    try:
                        data = existing["encrypted_screenshots"]
                        current_paths = json.loads(data) if isinstance(data, str) else data
                    except:
                        pass
                
                # Encrypt paths before DB save
                encrypted_new_paths = [crypto.encrypt(p) for p in new_paths]
                final_paths = current_paths + encrypted_new_paths
                
                await db.execute(
                    "UPDATE trades SET encrypted_screenshots = $1::jsonb WHERE id = $2",
                    json.dumps(final_paths), trade_id
                )
                uploaded_to_trade = True
        except Exception as e:
            logger.error(f"Failed to link screenshots: {e}")

    return {"files": uploaded_results, "uploaded_to_trade": uploaded_to_trade, "count": len(uploaded_results)}