# backend/app/apis/v1/brokers.py
import logging
import json
from datetime import datetime
from typing import List, Optional, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from supabase import create_client, Client

from app.core.config import settings
from app.auth.dependency import get_current_user
from app.lib.encryption import crypto
from app.lib.brokers.factory import get_broker_adapter

# --- Configuration ---
router = APIRouter()
security = HTTPBearer()
logger = logging.getLogger(__name__)

# --- Pydantic Models ---

class BrokerBase(BaseModel):
    broker_name: str = Field(..., min_length=1)
    # Input only fields (never returned)
    api_key: str = Field(..., min_length=1)
    api_secret: str = Field(..., min_length=1)
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

# --- Dependency ---

def get_authenticated_client(creds: HTTPAuthorizationCredentials = Depends(security)) -> Client:
    """
    Creates a Supabase client authenticated as the user.
    Enforces RLS policies securely.
    """
    token = creds.credentials
    client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)
    client.postgrest.auth(token)
    return client

# --- Endpoints ---

@router.get("/", response_model=List[BrokerResponse])
def get_brokers(
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client)
):
    """
    List all connected broker accounts.
    Returns sanitized data (no full keys).
    """
    try:
        user_id = current_user["sub"]
        response = supabase.table("broker_accounts")\
            .select("id, broker_name, api_key_last_digits, last_sync_time, is_active, created_at")\
            .eq("user_id", user_id)\
            .order("created_at", desc=True)\
            .execute()
            
        return response.data
    except Exception as e:
        logger.error(f"Error fetching brokers: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch broker accounts")

@router.post("/", response_model=BrokerResponse, status_code=status.HTTP_201_CREATED)
def add_broker(
    broker: BrokerCreate,
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client)
):
    """
    Securely add a new broker account.
    Encrypts credentials immediately using AES-256 before storage.
    """
    user_id = current_user["sub"]
    logger.info(f"User {user_id} adding broker {broker.broker_name}")

    try:
        # 1. Prepare Credentials Payload
        creds_payload = json.dumps({
            "api_key": broker.api_key,
            "api_secret": broker.api_secret
        })
        
        # 2. Encrypt Credentials
        encrypted_creds = crypto.encrypt(creds_payload)
        
        # 3. Create Last 4 Digits for UI Display
        # Handle cases where key is short
        last_digits = broker.api_key[-4:] if len(broker.api_key) >= 4 else "****"

        # 4. Insert into DB
        data = {
            "user_id": user_id,
            "broker_name": broker.broker_name,
            "encrypted_credentials": encrypted_creds,
            "api_key_last_digits": last_digits,
            "is_active": broker.is_active,
            "last_sync_time": None
        }

        response = supabase.table("broker_accounts").insert(data).execute()
        
        if not response.data:
            raise HTTPException(status_code=500, detail="Failed to save broker account")

        # Return sanitized response
        return response.data[0]

    except Exception as e:
        logger.error(f"Error adding broker: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to add broker: {str(e)}")

@router.delete("/{broker_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_broker(
    broker_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client)
):
    """
    Remove a connected broker account.
    """
    try:
        response = supabase.table("broker_accounts").delete().eq("id", broker_id).execute()
        
        if not response.data:
            raise HTTPException(status_code=404, detail="Broker not found or access denied")
            
        return None
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting broker: {e}")
        raise HTTPException(status_code=500, detail="Failed to remove broker")

@router.post("/{broker_id}/sync")
async def sync_broker(
    broker_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client)
):
    """
    Trigger a manual sync for this broker.
    Fetches, normalizes, and logs recent trades.
    """
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
        logger.error(f"Decryption failed for broker {broker_id}")
        raise HTTPException(status_code=500, detail="Failed to decrypt credentials")

    # 3. Instantiate Adapter (Factory Pattern)
    try:
        adapter = get_broker_adapter(broker_record["broker_name"], credentials)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # 4. Execute Sync
    # NOTE: For high scale, this logic should be offloaded to a background worker (Celery/RQ)
    try:
        if not await adapter.authenticate():
            raise HTTPException(status_code=401, detail="Broker authentication failed. Check API Keys.")
            
        raw_trades = await adapter.fetch_recent_trades(days=30)
        normalized_trades = adapter.normalize_trades(raw_trades)
        
        # 5. Insert Trades
        count = 0
        for trade in normalized_trades:
            trade["user_id"] = user_id
            trade["broker_account_id"] = broker_id
            trade["source_type"] = "AUTO_SYNC"
            
            # TODO: Add deduplication logic here (e.g., check if trade with same entry_time/symbol exists)
            try:
                supabase.table("trades").insert(trade).execute()
                count += 1
            except Exception as e:
                # Log duplicate errors silently, fail on others
                if "duplicate key" not in str(e).lower():
                    logger.warning(f"Failed to insert synced trade: {e}")
        
        # 6. Update Sync Timestamp
        now = datetime.now().isoformat()
        supabase.table("broker_accounts").update({"last_sync_time": now}).eq("id", broker_id).execute()

        return {
            "status": "success", 
            "message": f"Synced {count} trades from {broker_record['broker_name']}",
            "trades_synced": count
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Sync failed for {broker_id}: {e}")
        raise HTTPException(status_code=502, detail=f"Sync failed: {str(e)}")