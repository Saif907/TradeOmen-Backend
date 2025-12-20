# backend/app/apis/v1/trades.py

import logging
import json
import io
import csv
from uuid import uuid4
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional, Dict, Any, Union

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
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from supabase import create_client, Client

# --- Internal Imports ---
from app.core.config import settings
from app.auth.dependency import get_current_user
from app.lib.data_sanitizer import sanitizer
from app.lib.encryption import crypto
from app.services.quota_manager import QuotaManager
from app.services.plan_service import PlanService

# ✅ IMPORT CENTRALIZED SCHEMAS
from app.schemas import (
    TradeCreate,
    TradeUpdate,
    TradeResponse,
    PaginatedTradesResponse,
    PlanTier,
    InstrumentType,
    TradeSide,
    TradeStatus
)

# ---------------------------------------------------------------------
# Config & Setup
# ---------------------------------------------------------------------

router = APIRouter()
security = HTTPBearer()
logger = logging.getLogger(__name__)

# ✅ GLOBAL ADMIN CLIENT
# We initialize this ONCE to avoid creating a new connection for every trade in a list.
# This client is used ONLY for storage operations (Signing/Uploading) to bypass RLS.
supabase_admin: Client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)

# ---------------------------------------------------------------------
# Services (Local Helpers)
# ---------------------------------------------------------------------

class ScreenshotService:
    """Handles logic for paths, encryption, and dirty JSON."""

    @staticmethod
    def process_stored_screenshots(encrypted_val: Union[str, List, None]) -> List[Dict[str, Any]]:
        if not encrypted_val:
            return []

        # Normalize input to list
        try:
            raw_list = json.loads(encrypted_val) if isinstance(encrypted_val, str) else encrypted_val
            if not isinstance(raw_list, list):
                raw_list = [str(encrypted_val)]
        except:
            raw_list = [str(encrypted_val)]

        final_files = []
        
        for item in raw_list:
            try:
                # 1. Decrypt
                path = crypto.decrypt(item)
                if path == "[Decryption Error]":
                    path = item 

                # 2. Clean "Dirty" JSON or Prefixes
                path = ScreenshotService._clean_path(path)

                # 3. Generate URL using ADMIN client
                # We use supabase_admin to guarantee we don't get 400/403 RLS errors
                signed_url = ScreenshotService._get_signed_url(path)
                if signed_url:
                    final_files.append({"url": signed_url, "uploaded_at": None})
            
            except Exception as e:
                logger.warning(f"Screenshot processing error: {e}")
                
        return final_files

    @staticmethod
    def _clean_path(path: str) -> str:
        path = path.strip()
        if path.startswith("path:"):
            return path.replace("path:", "")

        if path.startswith("{"):
            try:
                meta = json.loads(path)
                if "files" in meta and meta["files"]:
                    return meta["files"][0].get("path", "")
                if "path" in meta:
                    return meta["path"]
            except json.JSONDecodeError:
                pass
        return path

    @staticmethod
    def _get_signed_url(path: str) -> Optional[str]:
        try:
            # ✅ FIX: Use Global Admin Client
            # This bypasses RLS policies that cause 400 Bad Request on the 'sign' endpoint
            res = supabase_admin.storage.from_(settings.SCREENSHOT_BUCKET).create_signed_url(path, 3600)
            
            if isinstance(res, dict):
                return res.get("signedURL") or res.get("signed_url")
            return res # Str
        except Exception as e:
            logger.error(f"Failed to sign URL for {path}: {e}")
            return None


# ---------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------

def get_authenticated_client(
    creds: HTTPAuthorizationCredentials = Depends(security)
) -> Client:
    # Use ANON key for RLS compliance unless admin access is strictly needed
    client = create_client(settings.SUPABASE_URL, settings.SUPABASE_ANON_KEY)
    client.postgrest.auth(creds.credentials)
    return client


# ---------------------------------------------------------------------
# 1. Export Route
# ---------------------------------------------------------------------

@router.get("/export", response_class=StreamingResponse)
def export_trades_csv(
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client)
):
    user_id = current_user["sub"]
    user_plan = PlanService.get_user_plan(user_id, supabase)
    
    live_profile = {**current_user, "plan_tier": user_plan}
    QuotaManager.check_feature_access(live_profile, "allow_csv_export")

    res = (
        supabase.table("trades")
        .select("*, strategies(name)")
        .eq("user_id", user_id)
        .order("entry_time", desc=True)
        .limit(10000)
        .execute()
    )
    
    trades = res.data or []
    output = io.StringIO()
    writer = csv.writer(output)
    
    headers = [
        "Date", "Time", "Symbol", "Type", "Side", "Status", 
        "Quantity", "Entry Price", "Exit Price", "PnL", 
        "Stop Loss", "Target", "Fees", "Notes", "Tags", "Strategy"
    ]
    writer.writerow(headers)

    for t in trades:
        entry_iso = t.get("entry_time") or ""
        try:
            dt = datetime.fromisoformat(entry_iso.replace("Z", "+00:00"))
            e_date, e_time = dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M:%S")
        except:
            e_date, e_time = entry_iso, ""

        notes = t.get("encrypted_notes")
        if notes:
            try: notes = crypto.decrypt(notes)
            except: pass

        writer.writerow([
            e_date, e_time, t.get("symbol"), t.get("instrument_type"),
            t.get("direction"), t.get("status"), t.get("quantity"),
            t.get("entry_price"), t.get("exit_price"), t.get("pnl"),
            t.get("stop_loss"), t.get("target"), t.get("fees"),
            notes, ", ".join(t.get("tags") or []),
            t.get("strategies", {}).get("name") if t.get("strategies") else "No Strategy"
        ])

    output.seek(0)
    filename = f"trades_{datetime.now().strftime('%Y%m%d')}.csv"
    
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


# ---------------------------------------------------------------------
# 2. Upload Route (Supports Multiple Files)
# ---------------------------------------------------------------------

@router.post("/uploads/trade-screenshots", status_code=201)
async def upload_trade_screenshots(
    request: Request,
    files: List[UploadFile] = File(...),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Supports uploading multiple screenshots at once.
    Only for PRO and FOUNDER plans.
    """
    user_id = current_user["sub"]
    
    # Check Plan using Admin Client (Reliable)
    user_plan = PlanService.get_user_plan(user_id, supabase_admin)

    if user_plan not in [PlanTier.PRO.value, PlanTier.FOUNDER.value]:
        raise HTTPException(status_code=403, detail="Upgrade to PRO to upload screenshots.")

    uploaded_results = []
    new_encrypted_paths = []

    # 2. Iterate Files
    for file in files:
        if file.content_type not in settings.ALLOWED_IMAGE_TYPES:
            continue 
        
        contents = await file.read()
        if len(contents) > settings.MAX_UPLOAD_SIZE_BYTES:
            continue

        ext = file.filename.split(".")[-1] if "." in file.filename else "png"
        filename = f"{uuid4().hex}.{ext}"
        path = f"{user_id}/{filename}"

        try:
            # ✅ Upload using Admin Client
            res = supabase_admin.storage.from_(settings.SCREENSHOT_BUCKET).upload(
                path, contents, {"content-type": file.content_type}
            )
            if res:
                new_encrypted_paths.append(crypto.encrypt(path))
                
                # ✅ Sign using Admin Client
                signed_url = ScreenshotService._get_signed_url(path)
                uploaded_results.append({"filename": file.filename, "url": signed_url or path})
        except Exception as e:
            logger.error(f"Failed to upload {file.filename}: {e}")

    # 3. Associate with Trade (Batch Update)
    trade_id = request.query_params.get("trade_id")
    uploaded_to_trade = False

    if trade_id and new_encrypted_paths:
        # Use Admin client to fetch trade to ensure we see it even if RLS is weird, 
        # though we check user_id manually below for safety.
        existing = supabase_admin.table("trades").select("encrypted_screenshots, user_id").eq("id", trade_id).single().execute()
        
        if existing.data and str(existing.data["user_id"]) == str(user_id):
            current_val = existing.data.get("encrypted_screenshots")
            current_list = []
            
            if current_val:
                try: 
                    current_list = json.loads(current_val) if isinstance(current_val, str) else current_val
                    if not isinstance(current_list, list): current_list = [str(current_val)]
                except: current_list = [str(current_val)]
            
            final_list = current_list + new_encrypted_paths
            
            supabase_admin.table("trades").update({
                "encrypted_screenshots": json.dumps(final_list)
            }).eq("id", trade_id).execute()
            uploaded_to_trade = True

    if not uploaded_results:
        raise HTTPException(status_code=400, detail="No valid files were uploaded.")

    return {
        "files": uploaded_results, 
        "uploaded_to_trade": uploaded_to_trade,
        "count": len(uploaded_results)
    }


# ---------------------------------------------------------------------
# 3. Trade CRUD
# ---------------------------------------------------------------------

@router.post("/", response_model=TradeResponse, status_code=201)
async def create_trade(
    trade: TradeCreate,
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client)
):
    user_id = current_user["sub"]
    user_plan = PlanService.get_user_plan(user_id, supabase)
    
    live_profile = {**current_user, "plan_tier": user_plan}
    await QuotaManager.check_trade_storage_limit(user_id, live_profile)

    tags = list(set(trade.tags or []))
    clean_notes = sanitizer.sanitize(trade.notes) if trade.notes else None
    
    enc_screenshots = None
    if trade.screenshots:
        enc_screenshots = json.dumps([crypto.encrypt(s) for s in trade.screenshots if s])

    trade_data = trade.model_dump(exclude={"notes", "screenshots"})
    trade_data.update({
        "user_id": user_id,
        "tags": tags,
        "pnl": trade.calculate_pnl(),
        "entry_time": trade.entry_time.isoformat(),
        "exit_time": trade.exit_time.isoformat() if trade.exit_time else None,
        "encrypted_notes": clean_notes,
        "encrypted_screenshots": enc_screenshots,
        "direction": trade.direction.value,
        "status": trade.status.value,
        "instrument_type": trade.instrument_type.value,
    })

    res = supabase.table("trades").insert(trade_data).execute()
    if not res.data:
        raise HTTPException(status_code=500, detail="Trade insert failed")

    return res.data[0]


@router.get("/", response_model=PaginatedTradesResponse)
def get_trades(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client)
):
    user_id = current_user["sub"]
    offset = (page - 1) * limit

    res = (
        supabase.table("trades")
        .select("*, strategies(name)", count="exact")
        .eq("user_id", user_id)
        .order("entry_time", desc=True)
        .range(offset, offset + limit - 1)
        .execute()
    )

    data = res.data or []
    total = res.count or 0

    # Decrypt notes
    for t in data:
        if t.get("encrypted_notes"):
            try:
                t["encrypted_notes"] = crypto.decrypt(t["encrypted_notes"])
            except:
                t["encrypted_notes"] = ""

    return {
        "data": data,
        "total": total,
        "page": page,
        "size": limit,
    }


@router.get("/{trade_id}", response_model=TradeResponse)
def get_trade(
    trade_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client),
):
    user_id = current_user["sub"]

    res = (
        supabase.table("trades")
        .select("*, strategies(name)")
        .eq("id", trade_id)
        .eq("user_id", user_id)
        .single()
        .execute()
    )

    if not res.data:
        raise HTTPException(status_code=404, detail="Trade not found")

    trade_data = res.data

    if trade_data.get("encrypted_notes"):
        try:
            trade_data["encrypted_notes"] = crypto.decrypt(trade_data["encrypted_notes"])
        except Exception as e:
            logger.warning(f"Failed to decrypt notes for trade {trade_id}: {e}")
            trade_data["encrypted_notes"] = ""

    return trade_data


@router.get("/{trade_id}/screenshots")
def get_trade_screenshots(
    trade_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client),
):
    user_id = current_user["sub"]

    # 1. Verify Ownership with User Client (RLS check)
    res = supabase.table("trades").select("encrypted_screenshots, user_id").eq("id", trade_id).single().execute()
    
    if not res.data:
        raise HTTPException(status_code=404, detail="Trade not found")
    if str(res.data.get("user_id")) != str(user_id):
        raise HTTPException(status_code=403, detail="Forbidden")

    # 2. Process Screenshots using Global Admin Client (in service)
    # We passed the ownership check above, so it's safe to use admin for signing now.
    files = ScreenshotService.process_stored_screenshots(res.data.get("encrypted_screenshots"))
    
    return {"files": files}


@router.put("/{trade_id}", response_model=TradeResponse)
async def update_trade(
    trade_id: str,
    payload: TradeUpdate,
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client)
):
    user_id = current_user["sub"]
    
    existing = supabase.table("trades").select("user_id").eq("id", trade_id).single().execute()
    if not existing.data:
        raise HTTPException(status_code=404, detail="Trade not found")
    if str(existing.data["user_id"]) != str(user_id):
        raise HTTPException(status_code=403, detail="Forbidden")

    update_data = payload.model_dump(exclude_unset=True)
    
    if "notes" in update_data:
        update_data["encrypted_notes"] = sanitizer.sanitize(update_data.pop("notes"))
    
    for field in ["entry_time", "exit_time"]:
        if field in update_data and isinstance(update_data[field], datetime):
            update_data[field] = update_data[field].isoformat()
            
    for field in ["direction", "status", "instrument_type"]:
        if field in update_data and isinstance(update_data[field], Enum):
            update_data[field] = update_data[field].value

    res = supabase.table("trades").update(update_data).eq("id", trade_id).execute()
    return res.data[0]


@router.delete("/{trade_id}", status_code=204)
def delete_trade(
    trade_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client)
):
    res = supabase.table("trades").delete().eq("id", trade_id).eq("user_id", current_user["sub"]).execute()
    
    if not res.data:
        check = supabase.table("trades").select("user_id").eq("id", trade_id).execute()
        if check.data:
            raise HTTPException(status_code=403, detail="Forbidden")
        raise HTTPException(status_code=404, detail="Trade not found")
        
    return {"detail": "deleted"}