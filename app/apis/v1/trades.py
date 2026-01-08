# backend/app/apis/v1/trades.py
import logging
import json
import io
import csv
from uuid import uuid4
from datetime import datetime
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
from starlette.concurrency import run_in_threadpool
from supabase import create_client, Client

from app.core.config import settings
from app.auth.dependency import get_current_user
from app.lib.data_sanitizer import sanitizer
from app.lib.encryption import crypto  # used only for legacy-decrypt attempts
from app.services.quota_manager import QuotaManager
from app.services.plan_service import PlanService

from app.schemas import (
    TradeCreate,
    TradeUpdate,
    TradeResponse,
    PaginatedTradesResponse,
    PlanTier,
)

logger = logging.getLogger("tradeomen.trades")
router = APIRouter()

# Admin client (only for storage operations like upload / signed urls)
supabase_admin: Client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)


# ---------------------------
# Helpers
# ---------------------------
def _uid(user: Dict[str, Any]) -> str:
    """Normalize a user identifier from JWT / DB profile to string."""
    uid = user.get("id") or user.get("sub")
    if not uid:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    return str(uid)


async def _sb(fn):
    """Run blocking Supabase SDK call in a threadpool to avoid blocking event loop."""
    return await run_in_threadpool(fn)


# ---------------------------
# Screenshot Service (storage-only)
# ---------------------------
class ScreenshotService:
    """
    Handles generating signed URLs for stored screenshot paths.
    New uploads store plain storage paths in 'screenshots' field as JSON list.
    For legacy entries stored in 'encrypted_screenshots', we attempt a best-effort decrypt
    (if crypto.decrypt is available) and otherwise treat the stored value as a plain path.
    """

    @staticmethod
    def normalize_list(value: Union[str, List, None]) -> List[str]:
        if not value:
            return []
        if isinstance(value, list):
            return value
        try:
            return json.loads(value) if isinstance(value, str) else [str(value)]
        except Exception:
            return [str(value)]

    @staticmethod
    def try_decrypt(item: str) -> str:
        """Attempt to decrypt legacy encrypted paths, but fallback to raw string on failure."""
        try:
            # crypto.decrypt should return decoded path or raise / return a sentinel
            out = crypto.decrypt(item)
            if out and out != "[Decryption Error]":
                return out
        except Exception:
            pass
        return item

    @staticmethod
    def _get_signed_url(path: str) -> Optional[str]:
        try:
            res = supabase_admin.storage.from_(settings.SCREENSHOT_BUCKET).create_signed_url(path, 3600)
            # return signedURL or signed_url key if present
            if isinstance(res, dict):
                return res.get("signedURL") or res.get("signed_url")
            return res  # sometimes returns string
        except Exception as e:
            logger.warning("Failed to create signed URL for %s: %s", path, e)
            return None

    @staticmethod
    def process_stored_screenshots(value: Union[str, List, None]) -> List[Dict[str, Any]]:
        """Return list of {"path": "<path>", "url": "<signed_url>"}"""
        paths = ScreenshotService.normalize_list(value)
        out = []
        for item in paths:
            try:
                # attempt decrypt for legacy storages; else keep raw
                path = ScreenshotService.try_decrypt(item)
                path = str(path).strip()
                if not path:
                    continue
                url = ScreenshotService._get_signed_url(path)
                out.append({"path": path, "url": url or path})
            except Exception as e:
                logger.debug("Error processing screenshot item: %s", e)
                continue
        return out


# ---------------------------
# Dependency: create a user-level supabase client
# ---------------------------
def get_authenticated_client(creds = Depends):
    """
    Note: This function is intentionally simple so the route-level dependency
    can call create_client(...) and set postgrest.auth(...).
    We will use run_in_threadpool when executing calls.
    """
    # For FastAPI dependency injection the actual signature in use above
    # is `supabase: Client = Depends(get_authenticated_client)` so FastAPI
    # will call this and pass appropriate credentials via lower-level security.
    def _inner(creds_obj):
        client = create_client(settings.SUPABASE_URL, settings.SUPABASE_ANON_KEY)
        # `creds_obj` will be an HTTPAuthorizationCredentials when wired with security
        # but if get_current_user is used, this dependency can be replaced by caller.
        try:
            # if there's an Authorization header (Bearer token), set auth
            if getattr(creds_obj, "credentials", None):
                client.postgrest.auth(creds_obj.credentials)
        except Exception:
            # if anything fails we still return the anon client (RLS enforced)
            pass
        return client
    return _inner


# ---------------------------
# 1) CSV Export (sanitized)
# ---------------------------
@router.get("/export", response_class=StreamingResponse)
async def export_trades_csv(
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(lambda: create_client(settings.SUPABASE_URL, settings.SUPABASE_ANON_KEY)),
):
    """
    Streams a CSV of trades. Notes are sanitized to avoid PII leaks.
    Only allowed if user's plan permits CSV export.
    """
    user_id = _uid(current_user)

    # Plan check using user-level client
    plan = PlanService.get_user_plan(user_id, supabase)
    QuotaManager.check_feature_access({**current_user, "plan_tier": plan}, "allow_export_csv")

    # Fetch up to a large but bounded number of trades (protect memory)
    def _query():
        return supabase.table("trades").select("*, strategies(name)").eq("user_id", user_id).order("entry_time", desc=True).limit(10000).execute()

    res = await _sb(_query)
    trades = res.data or []

    def csv_generator():
        buf = io.StringIO()
        writer = csv.writer(buf)
        headers = [
            "Date", "Time", "Symbol", "Type", "Side", "Status",
            "Quantity", "Entry Price", "Exit Price", "PnL",
            "Stop Loss", "Target", "Fees", "Notes", "Tags", "Strategy"
        ]
        writer.writerow(headers)
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate(0)

        for t in trades:
            entry_iso = t.get("entry_time") or ""
            try:
                dt = datetime.fromisoformat(entry_iso.replace("Z", "+00:00"))
                e_date, e_time = dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M:%S")
            except Exception:
                e_date, e_time = entry_iso, ""

            # Prefer plain notes (new schema). If not present, try legacy encrypted_notes and attempt decrypt.
            notes = ""
            if t.get("notes"):
                notes = sanitizer.sanitize(t.get("notes") or "")
            else:
                legacy = t.get("encrypted_notes")
                if legacy:
                    try:
                        # attempt legacy decrypt; if fails, treat as raw and sanitize
                        plain = crypto.decrypt(legacy)
                        notes = sanitizer.sanitize(plain if plain and plain != "[Decryption Error]" else legacy)
                    except Exception:
                        notes = sanitizer.sanitize(str(legacy))

            tags = ", ".join(t.get("tags") or [])
            strategy = t.get("strategies", {}).get("name") if t.get("strategies") else "No Strategy"

            row = [
                e_date, e_time, t.get("symbol"), t.get("instrument_type"),
                t.get("direction"), t.get("status"), t.get("quantity"),
                t.get("entry_price"), t.get("exit_price"), t.get("pnl"),
                t.get("stop_loss"), t.get("target"), t.get("fees"),
                notes, tags, strategy
            ]
            writer.writerow(row)
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)

    filename = f"trades_{datetime.now().strftime('%Y%m%d')}.csv"
    return StreamingResponse(csv_generator(), media_type="text/csv", headers={"Content-Disposition": f"attachment; filename={filename}"})


# ---------------------------
# 2) Upload screenshots (admin client used for storage only)
# ---------------------------
@router.post("/uploads/trade-screenshots", status_code=201)
async def upload_trade_screenshots(
    request: Request,
    files: List[UploadFile] = File(...),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Upload multiple images to Supabase storage bucket.
    New uploads store **plain paths** in the 'screenshots' JSON column.
    Only PRO / FOUNDER plan allowed.
    """
    user_id = _uid(current_user)
    user_plan = PlanService.get_user_plan(user_id, supabase_admin)

    if user_plan not in [PlanTier.PRO.value, PlanTier.FOUNDER.value]:
        raise HTTPException(status_code=403, detail="Upgrade to PRO to upload screenshots.")

    uploaded_results = []
    new_paths: List[str] = []

    for file in files:
        if file.content_type not in settings.ALLOWED_IMAGE_TYPES:
            logger.debug("Skipping file due to content type: %s", file.filename)
            continue

        contents = await file.read()
        if len(contents) > settings.MAX_UPLOAD_SIZE_BYTES:
            logger.debug("Skipping file due to size: %s", file.filename)
            continue

        ext = file.filename.split(".")[-1] if "." in file.filename else "png"
        filename = f"{uuid4().hex}.{ext}"
        path = f"{user_id}/{filename}"

        try:
            # Upload via admin client (blocking SDK)
            def _upload():
                return supabase_admin.storage.from_(settings.SCREENSHOT_BUCKET).upload(path, contents, {"content-type": file.content_type})
            await _sb(_upload)

            # create signed url for immediate return
            signed = ScreenshotService._get_signed_url(path)
            uploaded_results.append({"filename": file.filename, "path": path, "url": signed or path})
            new_paths.append(path)
        except Exception as e:
            logger.error("Upload failed for %s: %s", file.filename, e)
            continue

    # Optionally associate with a trade if trade_id provided
    trade_id = request.query_params.get("trade_id")
    uploaded_to_trade = False
    if trade_id and new_paths:
        try:
            # Fetch via admin client to avoid RLS issues but still verify ownership
            def _fetch():
                return supabase_admin.table("trades").select("screenshots, encrypted_screenshots, user_id").eq("id", trade_id).single().execute()
            existing = await _sb(_fetch)
            if existing.data and str(existing.data.get("user_id")) == str(user_id):
                # Merge existing screenshots (prefer 'screenshots' if present)
                current_list = []
                if existing.data.get("screenshots"):
                    try:
                        current_list = existing.data["screenshots"] if isinstance(existing.data["screenshots"], list) else json.loads(existing.data["screenshots"])
                    except Exception:
                        current_list = [existing.data["screenshots"]]
                else:
                    # fallback to legacy encrypted_screenshots (attempt decrypt)
                    legacy = existing.data.get("encrypted_screenshots")
                    if legacy:
                        try:
                            arr = json.loads(legacy) if isinstance(legacy, str) else legacy
                            # attempt to decrypt each
                            current_list = [ScreenshotService.try_decrypt(x) for x in (arr if isinstance(arr, list) else [arr])]
                        except Exception:
                            current_list = [str(legacy)]

                final_list = current_list + new_paths

                def _update():
                    return supabase_admin.table("trades").update({"screenshots": json.dumps(final_list)}).eq("id", trade_id).execute()

                await _sb(_update)
                uploaded_to_trade = True
        except Exception as e:
            logger.warning("Failed to associate screenshots to trade %s: %s", trade_id, e)

    if not uploaded_results:
        raise HTTPException(status_code=400, detail="No valid files were uploaded.")

    return {"files": uploaded_results, "uploaded_to_trade": uploaded_to_trade, "count": len(uploaded_results)}


# ---------------------------
# 3) Trade CRUD
# ---------------------------
@router.post("/", response_model=TradeResponse, status_code=201)
async def create_trade(
    trade: TradeCreate,
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(lambda: create_client(settings.SUPABASE_URL, settings.SUPABASE_ANON_KEY)),
):
    """
    Create a trade. Notes are sanitized and stored as plain 'notes'.
    Screenshots passed in the payload should be storage paths (not encrypted).
    """
    user_id = _uid(current_user)
    plan = PlanService.get_user_plan(user_id, supabase)
    await QuotaManager.check_trade_storage_limit(user_id, {**current_user, "plan_tier": plan})

    tags = list(set(trade.tags or []))
    notes_plain = sanitizer.sanitize(trade.notes) if trade.notes else None

    screenshots_val = None
    if trade.screenshots:
        # keep plain paths (no encryption)
        screenshots_val = json.dumps([s for s in trade.screenshots if s])

    trade_data = trade.model_dump(exclude={"notes", "screenshots"})
    trade_data.update({
        "user_id": user_id,
        "tags": tags,
        "pnl": trade.calculate_pnl(),
        "entry_time": trade.entry_time.isoformat(),
        "exit_time": trade.exit_time.isoformat() if trade.exit_time else None,
        "notes": notes_plain,
        "screenshots": screenshots_val,
        "direction": trade.direction.value,
        "status": trade.status.value,
        "instrument_type": trade.instrument_type.value,
    })

    def _insert():
        return supabase.table("trades").insert(trade_data).execute()

    res = await _sb(_insert)
    if not res.data:
        raise HTTPException(status_code=500, detail="Trade insert failed")

    return res.data[0]


@router.get("/", response_model=PaginatedTradesResponse)
async def get_trades(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(lambda: create_client(settings.SUPABASE_URL, settings.SUPABASE_ANON_KEY)),
):
    user_id = _uid(current_user)
    offset = (page - 1) * limit

    def _query():
        return supabase.table("trades").select("*, strategies(name)", count="exact").eq("user_id", user_id).order("entry_time", desc=True).range(offset, offset + limit - 1).execute()

    res = await _sb(_query)
    data = res.data or []
    total = res.count or 0

    # For backward compatibility: if notes absent, try legacy encrypted_notes field (attempt decrypt)
    for t in data:
        if not t.get("notes") and t.get("encrypted_notes"):
            try:
                plain = crypto.decrypt(t.get("encrypted_notes"))
                t["notes"] = plain if plain and plain != "[Decryption Error]" else ""
            except Exception:
                t["notes"] = ""

    return {"data": data, "total": total, "page": page, "size": limit}


@router.get("/{trade_id}", response_model=TradeResponse)
async def get_trade(
    trade_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(lambda: create_client(settings.SUPABASE_URL, settings.SUPABASE_ANON_KEY)),
):
    user_id = _uid(current_user)

    def _query():
        return supabase.table("trades").select("*, strategies(name)").eq("id", trade_id).eq("user_id", user_id).single().execute()

    res = await _sb(_query)
    if not res.data:
        raise HTTPException(status_code=404, detail="Trade not found")

    trade_data = res.data
    # Legacy decrypt fallback
    if not trade_data.get("notes") and trade_data.get("encrypted_notes"):
        try:
            plain = crypto.decrypt(trade_data.get("encrypted_notes"))
            trade_data["notes"] = plain if plain and plain != "[Decryption Error]" else ""
        except Exception:
            trade_data["notes"] = ""

    return trade_data


@router.get("/{trade_id}/screenshots")
async def get_trade_screenshots(
    trade_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(lambda: create_client(settings.SUPABASE_URL, settings.SUPABASE_ANON_KEY)),
):
    user_id = _uid(current_user)

    # verify ownership (RLS) first via user client
    def _query():
        return supabase.table("trades").select("screenshots, encrypted_screenshots, user_id").eq("id", trade_id).single().execute()

    res = await _sb(_query)
    if not res.data:
        raise HTTPException(status_code=404, detail="Trade not found")
    if str(res.data.get("user_id")) != str(user_id):
        raise HTTPException(status_code=403, detail="Forbidden")

    # Use admin client to create signed urls (we already verified ownership)
    files = ScreenshotService.process_stored_screenshots(res.data.get("screenshots") or res.data.get("encrypted_screenshots"))
    return {"files": files}


@router.put("/{trade_id}", response_model=TradeResponse)
async def update_trade(
    trade_id: str,
    payload: TradeUpdate,
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(lambda: create_client(settings.SUPABASE_URL, settings.SUPABASE_ANON_KEY)),
):
    user_id = _uid(current_user)

    def _fetch():
        return supabase.table("trades").select("user_id").eq("id", trade_id).single().execute()

    existing = await _sb(_fetch)
    if not existing.data:
        raise HTTPException(status_code=404, detail="Trade not found")
    if str(existing.data.get("user_id")) != str(user_id):
        raise HTTPException(status_code=403, detail="Forbidden")

    update_data = payload.model_dump(exclude_unset=True)

    # If notes present in update, sanitize and store into 'notes' (no encryption)
    if "notes" in update_data:
        update_data["notes"] = sanitizer.sanitize(update_data.pop("notes"))

    # normalize datetimes to iso string
    for field in ["entry_time", "exit_time"]:
        if field in update_data and isinstance(update_data[field], datetime):
            update_data[field] = update_data[field].isoformat()

    # normalize enum values if present
    for field in ["direction", "status", "instrument_type"]:
        if field in update_data:
            val = update_data[field]
            try:
                # If enum object, extract .value
                update_data[field] = val.value if hasattr(val, "value") else val
            except Exception:
                pass

    def _update():
        return supabase.table("trades").update(update_data).eq("id", trade_id).execute()

    res = await _sb(_update)
    if not res.data:
        raise HTTPException(status_code=500, detail="Failed to update trade")
    return res.data[0]


@router.delete("/{trade_id}", status_code=204)
async def delete_trade(
    trade_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(lambda: create_client(settings.SUPABASE_URL, settings.SUPABASE_ANON_KEY)),
):
    user_id = _uid(current_user)

    def _delete():
        return supabase.table("trades").delete().eq("id", trade_id).eq("user_id", user_id).execute()

    res = await _sb(_delete)
    if not res.data:
        # check if trade exists but owned by someone else
        def _check():
            return supabase.table("trades").select("user_id").eq("id", trade_id).execute()
        check = await _sb(_check)
        if check.data:
            raise HTTPException(status_code=403, detail="Forbidden")
        raise HTTPException(status_code=404, detail="Trade not found")

    return {"detail": "deleted"}
