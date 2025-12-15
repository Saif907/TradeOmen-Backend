# backend/app/apis/v1/trades.py

import logging
import json
import io
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
    File
)
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field, field_validator, model_validator
from supabase import create_client, Client

from app.core.config import settings
from app.auth.dependency import get_current_user
from app.lib.llm_client import llm_client
from app.lib.data_sanitizer import sanitizer

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

    tags: Optional[List[str]] = []
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict)
    strategy_id: Optional[str] = None

    # ✅ NEW
    screenshots: Optional[List[str]] = []

    @field_validator("symbol")
    @classmethod
    def uppercase_symbol(cls, v):
        return v.upper().strip()

    @field_validator("direction", "status", "instrument_type", mode="before")
    @classmethod
    def uppercase_enums(cls, v):
        return v.upper().strip() if v else v


class TradeCreate(TradeBase):
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
    screenshots: Optional[List[str]] = None


class TradeResponse(TradeBase):
    id: str
    user_id: str
    pnl: Optional[float]
    created_at: str
    notes: Optional[str] = Field(None, alias="encrypted_notes")


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
# Screenshot Upload Endpoint
# ---------------------------------------------------------------------

@router.post("/uploads/trade-screenshot", status_code=201)
async def upload_trade_screenshot(
    file: UploadFile = File(...),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(400, "Only image files are allowed")

    contents = await file.read()
    if len(contents) > 5 * 1024 * 1024:
        raise HTTPException(400, "Image too large (max 5MB)")

    user_id = current_user["sub"]
    ext = file.filename.rsplit(".", 1)[-1].lower()
    filename = f"{uuid4().hex}.{ext}"
    path = f"{user_id}/{filename}"

    try:
        supabase = create_client(
            settings.SUPABASE_URL,
            settings.SUPABASE_SERVICE_ROLE_KEY
        )

        # ✅ THIS is the important line
        result = supabase.storage.from_("trade-screenshots").upload(
            path,
            contents,
            {"content-type": file.content_type}
        )

        if result is None:
            raise Exception("Supabase upload returned None")

        # ✅ Robust public URL handling
        public_url = supabase.storage.from_("trade-screenshots").get_public_url(path)

        url = (
            public_url
            if isinstance(public_url, str)
            else public_url.get("publicURL") or public_url.get("public_url")
        )

        if not url:
            raise Exception("Failed to generate public URL")

        return {"url": url}

    except Exception as e:
        logger.error(f"Screenshot upload failed: {e}", exc_info=True)
        raise HTTPException(500, "Screenshot upload failed")


# ---------------------------------------------------------------------
# Trade Endpoints
# ---------------------------------------------------------------------

@router.post("/", response_model=TradeResponse, status_code=201)
async def create_trade(
    trade: TradeCreate,
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client)
):
    user_id = current_user["sub"]

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

    trade_data = trade.dict(exclude={"notes"})
    trade_data.update({
        "user_id": user_id,
        "tags": tags,
        "pnl": pnl,
        "entry_time": trade.entry_time.isoformat(),
        "exit_time": trade.exit_time.isoformat() if trade.exit_time else None,
        "encrypted_notes": clean_notes,
    })

    response = supabase.table("trades").insert(trade_data).execute()
    if not response.data:
        raise HTTPException(500, "Trade insert failed")

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
        .select("*", count="exact")
        .order("entry_time", desc=True)
        .range(offset, offset + limit - 1)
        .execute()
    )

    return {
        "data": res.data,
        "total": res.count or 0,
        "page": page,
        "size": limit,
    }
