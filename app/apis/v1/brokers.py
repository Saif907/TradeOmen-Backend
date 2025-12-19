# backend/app/apis/v1/brokers.py

import logging
import json
import secrets
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, status, Request
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from supabase import create_client, Client

from app.core.config import settings
from app.auth.dependency import get_current_user
from app.lib.encryption import crypto
from app.lib.brokers.factory import get_broker_adapter
from app.lib.brokers.dhan import DhanAdapter
from urllib.parse import quote
from app.services.quota_manager import QuotaManager

router = APIRouter()
security = HTTPBearer()
logger = logging.getLogger(__name__)


# -----------------------
# Pydantic Models
# -----------------------
class BrokerBase(BaseModel):
    broker_name: str = Field(..., min_length=1)
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    is_active: bool = True


class BrokerCreate(BrokerBase):
    pass


class BrokerResponse(BaseModel):
    id: str
    broker_name: str
    api_key_last_digits: str
    last_sync_time: Optional[str] = None
    is_active: bool
    created_at: str


# -----------------------
# Supabase Dependency
# -----------------------
def get_authenticated_client(
    creds: HTTPAuthorizationCredentials = Depends(security),
) -> Client:
    token = creds.credentials
    client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)
    client.postgrest.auth(token)
    return client


# -----------------------
# Basic endpoints (listing / manual add for API-key brokers)
# -----------------------
@router.get("/", response_model=List[BrokerResponse])
def get_brokers(
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client),
):
    try:
        user_id = current_user["sub"]
        res = (
            supabase.table("broker_accounts")
            .select("id, broker_name, api_key_last_digits, last_sync_time, is_active, created_at")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .execute()
        )
        return res.data
    except Exception as e:
        logger.exception("Error fetching brokers: %s", e)
        raise HTTPException(status_code=500, detail="Failed to fetch broker accounts")


@router.post("/", response_model=BrokerResponse, status_code=status.HTTP_201_CREATED)
def add_broker(
    broker: BrokerCreate,
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client),
):
    """
    Manual add for API-key based brokers only. Dhan must be connected via OAuth.
    """
    user_id = current_user["sub"]

    if "dhan" in broker.broker_name.lower():
        raise HTTPException(status_code=400, detail="Please use Connect Dhan (OAuth) to authenticate.")

    try:
        creds_payload = json.dumps({"api_key": broker.api_key, "api_secret": broker.api_secret})
        encrypted_creds = crypto.encrypt(creds_payload)
        last_digits = broker.api_key[-4:] if broker.api_key and len(broker.api_key) >= 4 else "****"

        data = {
            "user_id": user_id,
            "broker_name": broker.broker_name,
            "encrypted_credentials": encrypted_creds,
            "api_key_last_digits": last_digits,
            "is_active": broker.is_active,
            "last_sync_time": None,
        }

        res = supabase.table("broker_accounts").insert(data).execute()
        if not res.data:
            raise HTTPException(status_code=500, detail="Failed to save broker account")
        return res.data[0]
    except Exception as e:
        logger.exception("Error adding broker: %s", e)
        raise HTTPException(status_code=500, detail="Failed to add broker")


# -----------------------
# Dhan OAuth Flow
# -----------------------
def _build_state_for_user(user_id: str) -> str:
    """
    Build a short-lived encrypted state containing user_id and nonce to avoid CSRF.
    """
    payload = {"user_id": user_id, "nonce": secrets.token_urlsafe(16), "iat": datetime.utcnow().isoformat()}
    return crypto.encrypt(json.dumps(payload))


def _validate_state_for_user(state: str, expected_user_id: str) -> bool:
    try:
        raw = crypto.decrypt(state)
        payload = json.loads(raw)
        return str(payload.get("user_id")) == str(expected_user_id)
    except Exception:
        logger.exception("Invalid state value provided")
        return False


@router.get("/dhan/auth-url")
def get_dhan_auth_url(
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    if not settings.DHAN_CLIENT_ID or not settings.DHAN_REDIRECT_URI:
        raise HTTPException(status_code=500, detail="Dhan not configured on server")

    user_id = current_user["sub"]
    state = _build_state_for_user(user_id)

    # ✅ URL-ENCODE redirect URI (THIS WAS MISSING)
    encoded_redirect = quote(settings.DHAN_REDIRECT_URI, safe="")

    authorize_url = (
        "https://dhan.co/login"
        f"?clientId={settings.DHAN_CLIENT_ID}"
        f"&redirectUri={encoded_redirect}"
        f"&state={state}"
    )

    return {"url": authorize_url}



@router.get("/dhan/callback")
def dhan_callback(request: Request):
    """
    Browser redirect landing. Dhan redirects to the backend, which forwards the tokenId + state to frontend.
    The frontend should capture tokenId & state from the query and call the protected POST /dhan/connect endpoint.
    """
    params = request.query_params
    token_id = params.get("tokenId") or params.get("token_id") or params.get("code")
    state = params.get("state")
    if not token_id:
        # redirect to frontend with an error
        return RedirectResponse(f"{settings.FRONTEND_URL}/settings/accounts?error=dhan_missing_token")
    # Redirect user to frontend process route (frontend will POST tokenId+state to protected endpoint)
    redirect_to = f"{settings.FRONTEND_URL}/settings/accounts?tokenId={token_id}"
    if state:
        redirect_to += f"&state={state}"
    return RedirectResponse(redirect_to)


@router.post("/dhan/connect")
async def connect_dhan_broker(
    payload: Dict[str, str],
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client),
):
    """
    Backend endpoint to exchange tokenId -> access_token, validate it, and persist to Supabase.
    Protected by Bearer auth (frontend must call this while the user is authenticated).
    payload: { "tokenId": "...", "state": "..." }
    """
    user_id = current_user["sub"]
    token_id = payload.get("tokenId") or payload.get("token_id")
    state = payload.get("state")

    if not token_id:
        raise HTTPException(status_code=400, detail="tokenId is required")

    # Validate state ties to this user
    if not state or not _validate_state_for_user(state, user_id):
        raise HTTPException(status_code=400, detail="Invalid or missing state parameter")

    # Exchange token
    exchanged = await DhanAdapter.exchange_token(token_id)
    if not exchanged or not exchanged.get("access_token"):
        logger.error("Dhan token exchange failed for user %s", user_id)
        raise HTTPException(status_code=400, detail="Failed to exchange token with Dhan")

    access_token = exchanged["access_token"]
    expires_at = exchanged.get("expires_at")

    # Validate token right away
    adapter = DhanAdapter({"access_token": access_token})
    ok = await adapter.authenticate()
    if not ok:
        raise HTTPException(status_code=400, detail="Dhan token validation failed after exchange")

    # Persist encrypted credentials to Supabase (upsert semantics)
    creds_obj = {"access_token": access_token}
    if expires_at:
        creds_obj["expires_at"] = expires_at
    creds_json = json.dumps(creds_obj)
    encrypted = crypto.encrypt(creds_json)

    # Upsert: update if exists else insert
    try:
        # Try to find an existing Dhan record for this user
        existing = (
            supabase.table("broker_accounts")
            .select("id")
            .eq("user_id", user_id)
            .eq("broker_name", "Dhan")
            .execute()
        )
        now = datetime.utcnow().isoformat()
        data = {
            "user_id": user_id,
            "broker_name": "Dhan",
            "encrypted_credentials": encrypted,
            "api_key_last_digits": "OAUTH",
            "is_active": True,
            "last_sync_time": None,
            "updated_at": now,
            "created_at": now,
        }

        if existing.data:
            supabase.table("broker_accounts").update(
                {
                    "encrypted_credentials": encrypted,
                    "api_key_last_digits": "OAUTH",
                    "is_active": True,
                    "last_sync_time": None,
                    "updated_at": now,
                }
            ).eq("id", existing.data[0]["id"]).execute()
            broker_id = existing.data[0]["id"]
        else:
            res = supabase.table("broker_accounts").insert(data).execute()
            broker_id = res.data[0]["id"]

        return {"status": "ok", "broker_id": broker_id}
    except Exception as e:
        logger.exception("Failed to persist Dhan broker for user %s: %s", user_id, e)
        raise HTTPException(status_code=500, detail="Failed to persist Dhan credentials")


# -----------------------
# Delete & sync (unchanged but robustified)
# -----------------------
@router.delete("/{broker_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_broker(
    broker_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client),
):
    try:
        res = supabase.table("broker_accounts").delete().eq("id", broker_id).eq("user_id", current_user["sub"]).execute()
        if not res.data:
            raise HTTPException(status_code=404, detail="Broker not found or access denied")
        return None
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error deleting broker: %s", e)
        raise HTTPException(status_code=500, detail="Failed to remove broker")


@router.post("/{broker_id}/sync")
async def sync_broker(
    broker_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client),
):
    QuotaManager.check_feature_access(current_user, "allow_broker_sync")
    user_id = current_user["sub"]

    # 1. Fetch Broker Record
    res = supabase.table("broker_accounts").select("*").eq("id", broker_id).eq("user_id", user_id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Broker not found")

    broker_record = res.data[0]

    # 2. Decrypt Credentials
    try:
        decrypted_json = crypto.decrypt(broker_record["encrypted_credentials"])
        credentials = json.loads(decrypted_json)
    except Exception:
        logger.exception("Decryption failed for broker %s", broker_id)
        raise HTTPException(status_code=500, detail="Failed to decrypt credentials")

    # Dhan must have access_token
    if "dhan" in broker_record["broker_name"].lower():
        if "access_token" not in credentials:
            raise HTTPException(status_code=401, detail="Dhan not connected; please reconnect via OAuth")

    # 3. Instantiate adapter
    try:
        adapter = get_broker_adapter(broker_record["broker_name"], credentials)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # 4. Authenticate
    if not await adapter.authenticate():
        if "dhan" in broker_record["broker_name"].lower():
            raise HTTPException(status_code=401, detail="Dhan session invalid/expired; please reconnect")
        raise HTTPException(status_code=401, detail="Broker authentication failed")

    # 5. Fetch & normalize trades
    try:
        raw_trades = await adapter.fetch_recent_trades(days=30)
        normalized_trades = adapter.normalize_trades(raw_trades)
    except Exception as e:
        logger.exception("Error fetching or normalizing trades: %s", e)
        raise HTTPException(status_code=502, detail="Failed to fetch trades from broker")

    # 6. Insert trades (best effort)
    inserted = 0
    for trade in normalized_trades:
        trade["user_id"] = user_id
        trade["broker_account_id"] = broker_id
        trade["source_type"] = "AUTO_SYNC"
        try:
            supabase.table("trades").insert(trade).execute()
            inserted += 1
        except Exception as e:
            if "duplicate" not in str(e).lower():
                logger.warning("Failed to insert trade: %s", e)

    # 7. Update sync timestamp
    now = datetime.utcnow().isoformat()
    supabase.table("broker_accounts").update({"last_sync_time": now}).eq("id", broker_id).execute()

    return {"status": "success", "message": f"Synced {inserted} trades from {broker_record['broker_name']}", "trades_synced": inserted}
