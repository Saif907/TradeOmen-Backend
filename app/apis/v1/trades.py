# backend/app/apis/v1/trades.py

import logging
import json
import io
import csv
import time
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
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field, field_validator, model_validator
from supabase import create_client, Client

from app.core.config import settings
from app.auth.dependency import get_current_user
from app.lib.llm_client import llm_client
from app.lib.data_sanitizer import sanitizer
from app.lib.encryption import crypto
from app.services.quota_manager import QuotaManager


# ---------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------

router = APIRouter()
security = HTTPBearer()
logger = logging.getLogger(__name__)

SCREENSHOT_BUCKET = "trade-screenshots"
MAX_IMAGE_SIZE = 5 * 1024 * 1024  # 5MB
ALLOWED_IMAGE_TYPES = {
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/webp",
}

# ---------------------------------------------------------------------
# Latency & Plan Caching Service (Local Helper)
# ---------------------------------------------------------------------

# Cache Store: { "user_uuid": { "plan": "PRO", "expires_at": 1700000000 } }
_PLAN_CACHE: Dict[str, Dict[str, Any]] = {}
CACHE_TTL = 60  # Cache plan for 60 seconds to solve US-India latency

class PlanService:
    @staticmethod
    def get_user_plan(user_id: str, supabase: Client) -> str:
        """
        Fetches the user's plan. 
        1. Checks in-memory cache (Instant).
        2. If missing/expired, fetches from DB (Slow ~250ms).
        3. Updates cache.
        """
        now = time.time()
        
        # 1. Check Cache
        cached_data = _PLAN_CACHE.get(user_id)
        if cached_data and cached_data["expires_at"] > now:
            return cached_data["plan"]

        # 2. Cache Miss - Fetch from DB
        try:
            res = supabase.table("user_profiles").select("plan_tier").eq("id", user_id).single().execute()
            
            real_plan = "FREE"
            if res.data and res.data.get("plan_tier"):
                real_plan = res.data.get("plan_tier").upper()
            
            # 3. Save to Cache
            _PLAN_CACHE[user_id] = {
                "plan": real_plan,
                "expires_at": now + CACHE_TTL
            }
            return real_plan

        except Exception as e:
            logger.error(f"Failed to fetch plan for {user_id}: {e}")
            return "FREE"

# ---------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------

class TradeBase(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=20)
    instrument_type: str = Field("STOCK", pattern="^(STOCK|CRYPTO|FOREX|FUTURES)$")
    direction: str = Field(..., pattern="^(?i)(LONG|SHORT)$")
    status: str = Field("OPEN", pattern="^(?i)(OPEN|CLOSED)$")

    entry_price: float = Field(..., gt=0)
    quantity: float = Field(..., gt=0)

    exit_price: Optional[float] = Field(None, gt=0)
    stop_loss: Optional[float] = Field(None, gt=0)
    target: Optional[float] = Field(None, gt=0)

    entry_time: datetime
    exit_time: Optional[datetime] = None

    fees: float = Field(0.0, ge=0)

    encrypted_notes: Optional[str] = None
    notes: Optional[str] = None

    tags: Optional[List[str]] = Field(default_factory=list)
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict)
    strategy_id: Optional[str] = None

    encrypted_screenshots: Optional[str] = None

    @field_validator("symbol")
    @classmethod
    def uppercase_symbol(cls, v):
        return v.upper().strip()

    @field_validator("direction", "status", "instrument_type", mode="before")
    @classmethod
    def uppercase_enums(cls, v):
        return v.upper().strip() if v else v


class TradeCreate(TradeBase):
    screenshots: Optional[List[str]] = None

    @model_validator(mode="after")
    def validate_trade(self):
        if self.exit_time and self.exit_time < self.entry_time:
            raise ValueError("Exit time cannot be before entry time.")

        if self.status == "CLOSED":
            if not self.exit_price:
                raise ValueError("Closed trades require exit price.")
            if not self.exit_time:
                raise ValueError("Closed trades require exit time.")

        return self


class TradeUpdate(BaseModel):
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
    tags: Optional[List[str]] = None
    metadata: Optional[Dict[str, Any]] = None
    strategy_id: Optional[str] = None
    encrypted_screenshots: Optional[str] = None


class TradeResponse(TradeBase):
    id: str
    user_id: str
    pnl: Optional[float]
    created_at: str
    notes: Optional[str] = Field(None, alias="encrypted_notes")
    strategies: Optional[Dict[str, Any]] = None


class PaginatedTradesResponse(BaseModel):
    data: List[TradeResponse]
    total: int
    page: int
    size: int


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

async def extract_tags_and_mistakes(notes: str) -> Dict[str, Any]:
    if not notes or len(notes) < 5:
        return {"tags": [], "mistakes": []}

    system_prompt = """
    You are a Trading Psychology Coach.
    Extract:
    - technical tags
    - psychological mistakes
    Return JSON: {"tags": [], "mistakes": []}
    """

    try:
        safe_notes = sanitizer.sanitize(notes)
        response = await llm_client.generate_response(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": safe_notes}
            ],
            model="gemini-2.5-flash",
            provider="gemini",
            response_format={"type": "json_object"}
        )
        return json.loads(response["content"])
    except Exception as e:
        logger.warning(f"AI extraction failed: {e}")
        return {"tags": [], "mistakes": []}


def get_authenticated_client(
    creds: HTTPAuthorizationCredentials = Depends(security)
) -> Client:
    client = create_client(
        settings.SUPABASE_URL,
        settings.SUPABASE_SERVICE_ROLE_KEY
    )
    client.postgrest.auth(creds.credentials)
    return client


# ---------------------------------------------------------------------
# 1. Export Route (With Cache & Live Plan Check)
# ---------------------------------------------------------------------
@router.get("/export", response_class=StreamingResponse)
def export_trades_csv(
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client)
):
    """
    Exports all trades. Checks Live DB Plan via Cache.
    """
    user_id = current_user["sub"]

    # 1. Get Plan (Cached)
    user_plan = PlanService.get_user_plan(user_id, supabase)

    # 2. Inject into profile for QuotaManager
    live_profile = {**current_user, "plan_tier": user_plan}
    
    # 3. Check Permissions
    QuotaManager.check_feature_access(live_profile, "allow_csv_export")

    # Fetch Data
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
        entry_iso = t.get("entry_time")
        entry_date = ""
        entry_time = ""
        if entry_iso:
            try:
                dt = datetime.fromisoformat(entry_iso.replace("Z", "+00:00"))
                entry_date = dt.strftime("%Y-%m-%d")
                entry_time = dt.strftime("%H:%M:%S")
            except ValueError:
                entry_date = entry_iso

        raw_notes = t.get("encrypted_notes") or ""
        decrypted_notes = raw_notes
        if raw_notes:
            try:
                decrypted_notes = crypto.decrypt(raw_notes)
            except Exception:
                pass

        strategy_data = t.get("strategies")
        strategy_name = strategy_data.get("name") if strategy_data else "No Strategy"

        writer.writerow([
            entry_date,
            entry_time,
            t.get("symbol"),
            t.get("instrument_type"),
            t.get("direction"),
            t.get("status"),
            t.get("quantity"),
            t.get("entry_price"),
            t.get("exit_price"),
            t.get("pnl"),
            t.get("stop_loss"),
            t.get("target"),
            t.get("fees"),
            decrypted_notes,
            ", ".join(t.get("tags") or []),
            strategy_name 
        ])

    output.seek(0)
    filename = f"my_trades_export_{datetime.now().strftime('%Y%m%d')}.csv"
    
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

# ---------------------------------------------------------------------
# 2. Screenshot Upload (Pro Restricted + Cached Check)
# ---------------------------------------------------------------------

@router.post("/uploads/trade-screenshot", status_code=201)
async def upload_trade_screenshot(
    request: Request,
    file: UploadFile = File(...),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Upload a screenshot to the SCREENSHOT_BUCKET.
    RESTRICTED: Only available to PRO and FOUNDER plans.
    """
    user_id = current_user["sub"]

    # 1. Initialize Client
    supabase = create_client(
        settings.SUPABASE_URL,
        settings.SUPABASE_SERVICE_ROLE_KEY
    )

    # 2. FAST CHECK (Uses Cache, solves Latency)
    user_plan = PlanService.get_user_plan(user_id, supabase)

    # 3. Reject Free Users Immediately
    if user_plan not in ["PRO", "FOUNDER"]:
        raise HTTPException(
            status_code=403, 
            detail="Screenshot uploads are a Pro feature. Please upgrade."
        )

    # --- Start Upload ---
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Only image files are allowed")

    contents = await file.read()
    if len(contents) > MAX_IMAGE_SIZE:
        raise HTTPException(status_code=400, detail="Image too large (max 5MB)")

    trade_id = request.query_params.get("trade_id")
    ext = (file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else "png")
    filename = f"{uuid4().hex}.{ext}"
    path = f"{user_id}/{filename}"

    try:
        res = supabase.storage.from_(SCREENSHOT_BUCKET).upload(
            path,
            contents,
            {"content-type": file.content_type}
        )

        if not res or (isinstance(res, dict) and res.get("error")):
            logger.error("Supabase upload error: %s", res)
            raise Exception("Supabase storage upload failed")

        signed_url = None
        try:
            signed_resp = supabase.storage.from_(SCREENSHOT_BUCKET).create_signed_url(path, 3600)
            if isinstance(signed_resp, dict):
                signed_url = signed_resp.get("signedURL") or signed_resp.get("signed_url")
            elif isinstance(signed_resp, str):
                signed_url = signed_resp
        except Exception:
            pass

        uploaded_to_trade = False

        if trade_id:
            existing_resp = supabase.table("trades").select("encrypted_screenshots, user_id").eq("id", trade_id).single().execute()
            tdata = existing_resp.data if getattr(existing_resp, "data", None) else None

            if tdata and str(tdata.get("user_id")) == str(user_id):
                current_val = tdata.get("encrypted_screenshots")
                current_list = []
                
                if current_val:
                    try:
                        parsed = json.loads(current_val)
                        if isinstance(parsed, list):
                            current_list = parsed
                        else:
                            current_list = [str(current_val)]
                    except json.JSONDecodeError:
                        current_list = [str(current_val)]

                encrypted_path = crypto.encrypt(path)
                current_list.append(encrypted_path)
                
                new_val = json.dumps(current_list)

                update_resp = supabase.table("trades").update({"encrypted_screenshots": new_val}).eq("id", trade_id).execute()
                if getattr(update_resp, "data", None):
                    uploaded_to_trade = True

        return {"url": signed_url or path, "uploaded_to_trade": uploaded_to_trade}

    except Exception as e:
        logger.error(f"Screenshot upload failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Screenshot upload failed")


# ---------------------------------------------------------------------
# 3. Create Trade (With Limits & Cache)
# ---------------------------------------------------------------------
@router.post("/", response_model=TradeResponse, status_code=201)
async def create_trade(
    trade: TradeCreate,
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client)
):
    user_id = current_user["sub"]

    # 1. Get Plan (Cached)
    user_plan = PlanService.get_user_plan(user_id, supabase)

    # 2. Inject into profile for QuotaManager
    live_profile = {**current_user, "plan_tier": user_plan}
    
    # 3. Check Limits
    await QuotaManager.check_trade_storage_limit(user_id, live_profile)

    clean_notes = sanitizer.sanitize(trade.notes) if trade.notes else None
    ai_tags, mistakes = [], []

    if clean_notes:
        analysis = await extract_tags_and_mistakes(clean_notes)
        ai_tags = analysis.get("tags", [])
        mistakes = analysis.get("mistakes", [])

    tags = list(set((trade.tags or []) + ai_tags + mistakes))

    pnl = None
    if trade.exit_price:
        mult = 1 if trade.direction == "LONG" else -1
        pnl = (
            (trade.exit_price - trade.entry_price)
            * trade.quantity
            * mult
            - trade.fees
        )

    encrypted_screenshots_json = None
    if trade.screenshots:
        encrypted_list = [crypto.encrypt(s) for s in trade.screenshots if s]
        encrypted_screenshots_json = json.dumps(encrypted_list)

    trade_data = trade.dict(exclude={"notes", "screenshots"})
    trade_data.update({
        "user_id": user_id,
        "tags": tags,
        "pnl": pnl,
        "entry_time": trade.entry_time.isoformat(),
        "exit_time": trade.exit_time.isoformat() if trade.exit_time else None,
        "encrypted_notes": clean_notes,
        "encrypted_screenshots": encrypted_screenshots_json,
    })

    response = supabase.table("trades").insert(trade_data).execute()
    if not getattr(response, "data", None):
        raise HTTPException(status_code=500, detail="Trade insert failed")

    return response.data[0]


@router.get("/", response_model=PaginatedTradesResponse)
def get_trades(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client)
):
    offset = (page - 1) * limit
    res = (
        supabase.table("trades")
        .select("*, strategies(name)", count="exact")
        .order("entry_time", desc=True)
        .range(offset, offset + limit - 1)
        .execute()
    )

    data = getattr(res, "data", None) or []
    total = int(getattr(res, "count", 0) or 0)

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

    if not getattr(res, "data", None):
        raise HTTPException(status_code=404, detail="Trade not found")

    return res.data


@router.get("/{trade_id}/screenshots")
def get_trade_screenshots(
    trade_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client),
):
    """
    Decrypts paths and handles 'dirty' JSON metadata.
    """
    user_id = current_user["sub"]

    res = (
        supabase.table("trades")
        .select("encrypted_screenshots, user_id")
        .eq("id", trade_id)
        .single()
        .execute()
    )

    if not getattr(res, "data", None):
        raise HTTPException(status_code=404, detail="Trade not found")

    tdata = res.data
    if str(tdata.get("user_id")) != str(user_id):
        raise HTTPException(status_code=403, detail="Forbidden")

    screenshots_val = tdata.get("encrypted_screenshots")
    encrypted_list = []
    
    if screenshots_val:
        try:
            parsed = json.loads(screenshots_val)
            if isinstance(parsed, list):
                encrypted_list = parsed
            else:
                encrypted_list = [str(screenshots_val)]
        except Exception:
            encrypted_list = [str(screenshots_val)]

    final_files = []
    for enc_item in encrypted_list:
        try:
            # 1. Decrypt
            path = crypto.decrypt(enc_item)
            if path == "[Decryption Error]":
                path = enc_item 

            # 2. Check for dirty JSON
            if path.strip().startswith("{"):
                try:
                    metadata = json.loads(path)
                    if "files" in metadata and isinstance(metadata["files"], list) and len(metadata["files"]) > 0:
                        potential_path = metadata["files"][0].get("path")
                        if potential_path:
                            path = potential_path
                    elif "path" in metadata:
                        path = metadata["path"]
                except json.JSONDecodeError:
                    pass

            # 3. Clean prefixes
            if path.startswith("path:"):
                path = path.replace("path:", "")
            
            # 4. Generate Signed URL
            signed_resp = supabase.storage.from_(SCREENSHOT_BUCKET).create_signed_url(path, 3600)
            
            signed_url = None
            if isinstance(signed_resp, dict):
                signed_url = signed_resp.get("signedURL") or signed_resp.get("signed_url")
            elif isinstance(signed_resp, str):
                signed_url = signed_resp
            
            if signed_url:
                final_files.append({"url": signed_url, "uploaded_at": None})
            else:
                logger.warning(f"Could not sign URL for path: {path}")

        except Exception as e:
            logger.warning(f"Error processing screenshot item: {e}")

    return {"files": final_files}


@router.put("/{trade_id}", response_model=TradeResponse)
async def update_trade(
    trade_id: str,
    payload: TradeUpdate,
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client)
):
    user_id = current_user["sub"]

    existing = supabase.table("trades").select("user_id").eq("id", trade_id).single().execute()
    if not getattr(existing, "data", None):
        raise HTTPException(status_code=404, detail="Trade not found")

    if str(existing.data.get("user_id")) != str(user_id):
        raise HTTPException(status_code=403, detail="Forbidden")

    update_data = payload.dict(exclude_unset=True)
    
    if "notes" in update_data:
        update_data["encrypted_notes"] = sanitizer.sanitize(update_data.pop("notes"))

    if "entry_time" in update_data and isinstance(update_data["entry_time"], datetime):
        update_data["entry_time"] = update_data["entry_time"].isoformat()
    if "exit_time" in update_data and isinstance(update_data["exit_time"], datetime):
        update_data["exit_time"] = update_data["exit_time"].isoformat()

    resp = supabase.table("trades").update(update_data).eq("id", trade_id).execute()
    if not getattr(resp, "data", None):
        raise HTTPException(status_code=500, detail="Trade update failed")

    return resp.data[0]


@router.delete("/{trade_id}", status_code=204)
def delete_trade(
    trade_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client)
):
    user_id = current_user["sub"]

    existing = supabase.table("trades").select("user_id").eq("id", trade_id).single().execute()
    if not getattr(existing, "data", None):
        raise HTTPException(status_code=404, detail="Trade not found")

    if str(existing.data.get("user_id")) != str(user_id):
        raise HTTPException(status_code=403, detail="Forbidden")

    supabase.table("trades").delete().eq("id", trade_id).execute()
    
    return {"detail": "deleted"}