from fastapi import APIRouter, Depends, HTTPException, status
from typing import List
from postgrest.base_request_builder import SingleAPIResponse

from ..libs import schemas
from ..libs.config import settings
from ..libs.security import encrypt_data, decrypt_data
from ..libs.supabase_client import get_supabase_client, Client
from ..auth.dependencies import get_current_user, check_plan_access, get_user_supabase_client

# Initialize the router
router = APIRouter()

# Dependency for authentication
AuthUser = Depends(get_current_user)

# Dependency for Supabase Client
SupabaseClient = Depends(get_user_supabase_client)

# --- CRUD Endpoints ---

@router.post(
    "/",
    response_model=schemas.TradeResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(check_plan_access(schemas.PlanFeature.MAX_TRADES_MONTH))]
)
async def create_trade(
    trade_data: schemas.TradeCreate,
    current_user: schemas.UserInDB = AuthUser,
    supabase_client: Client = SupabaseClient,
):
    """
    Creates a new trade entry, encrypts notes (Base64 string), and logs to Supabase.
    """
    # 1. Prepare Data Structure: mode='json' serializes datetimes to ISO strings
    trade_in = trade_data.model_dump(mode='json') 
    trade_in['user_id'] = current_user.user_id
    
    # 2. Handle Sensitive Notes and Encryption
    raw_notes = trade_in.pop('notes', None)

    if raw_notes:
        # ENCRYPTION: encrypt_data now returns a Base64 STRING
        trade_in['notes_encrypted'] = encrypt_data(raw_notes)
        
    # 3. Insert trade into Supabase
    try:
        response: SingleAPIResponse = supabase_client.table("trades").insert(trade_in).execute()

        if not response.data:
             raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Database failed to return the created trade.",
            )

        # 4. Decrypt notes for the final response to the user
        created_trade_data = response.data[0]
        # Decrypt expects the Base64 string from the database
        encrypted_notes_str = created_trade_data.pop('notes_encrypted', None)
        if encrypted_notes_str:
            created_trade_data['notes'] = decrypt_data(encrypted_notes_str)
            
        return schemas.TradeResponse(**created_trade_data)

    except Exception as e:
        error_detail = str(e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An error occurred while creating the trade: {error_detail}",
        )


@router.get(
    "/",
    response_model=List[schemas.TradeResponse],
)
async def read_trades(
    current_user: schemas.UserInDB = AuthUser,
    supabase_client: Client = SupabaseClient,
):
    """
    Retrieves all trades belonging to the authenticated user and decrypts notes.
    """
    try:
        response: SingleAPIResponse = supabase_client.table("trades").select("*").order("entry_datetime", desc=True).execute()
        
        trades_list = []
        for trade_data in response.data:
            # 2. DECRYPTION: Decrypt notes (Base64 string from DB)
            encrypted_notes_str = trade_data.pop('notes_encrypted', None)
            trade_data['notes'] = decrypt_data(encrypted_notes_str) if encrypted_notes_str else None
            
            # 3. Validate and add to list
            trades_list.append(schemas.TradeResponse(**trade_data))
            
        return trades_list

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An error occurred while fetching trades: {e}",
        )
        
@router.get(
    "/{trade_id}",
    response_model=schemas.TradeResponse,
)
async def read_trade(
    trade_id: str,
    current_user: schemas.UserInDB = AuthUser,
    supabase_client: Client = SupabaseClient,
):
    """
    Retrieves a single trade by ID, decrypts notes, and ensures ownership.
    """
    try:
        response: SingleAPIResponse = (
            supabase_client.table("trades")
            .select("*")
            .eq("id", trade_id)
            .single()
            .execute()
        )
        
        if not response.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Trade not found or you do not have permission to access it.",
            )
            
        # 1. DECRYPTION: Decrypt notes (Base64 string from DB)
        trade_data = response.data
        encrypted_notes_str = trade_data.pop('notes_encrypted', None)
        trade_data['notes'] = decrypt_data(encrypted_notes_str) if encrypted_notes_str else None
            
        return schemas.TradeResponse(**trade_data)

    except Exception as e:
        if hasattr(e, 'message') and ('not found' in e.message or 'None is not subscriptable' in e.message):
             raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Trade not found or you do not have permission to access it.",
            )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An error occurred while fetching the trade: {e}",
        )


@router.put(
    "/{trade_id}",
    response_model=schemas.TradeResponse,
)
async def update_trade(
    trade_id: str,
    trade_data: schemas.TradeCreate,
    current_user: schemas.UserInDB = AuthUser,
    supabase_client: Client = SupabaseClient,
):
    """
    Updates an existing trade, re-encrypting notes if they are present.
    """
    # 1. Prepare Data Structure: mode='json' serializes datetimes to ISO strings
    update_data = trade_data.model_dump(exclude_unset=True, mode='json')
    
    # 2. Handle notes update with re-encryption
    if 'notes' in update_data:
        raw_notes = update_data.pop('notes')
        if raw_notes:
            # Re-encryption to Base64 string
            update_data['notes_encrypted'] = encrypt_data(raw_notes)
        else:
            # Handle clearing notes
            update_data['notes_encrypted'] = None
    
    try:
        response: SingleAPIResponse = (
            supabase_client.table("trades")
            .update(update_data)
            .eq("id", trade_id)
            .execute()
        )

        if not response.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Trade not found or you do not have permission to update it.",
            )
            
        # Decrypt notes for response
        updated_trade_data = response.data[0]
        encrypted_notes_str = updated_trade_data.pop('notes_encrypted', None)
        updated_trade_data['notes'] = decrypt_data(encrypted_notes_str) if encrypted_notes_str else None
            
        return schemas.TradeResponse(**updated_trade_data)

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An error occurred while updating the trade: {e}",
        )


@router.delete(
    "/{trade_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_trade(
    trade_id: str,
    current_user: schemas.UserInDB = AuthUser,
    supabase_client: Client = SupabaseClient,
):
    """
    Deletes a trade, enforcing ownership and RLS.
    """
    try:
        # Delete the record where ID matches (RLS ensures ownership)
        supabase_client.table("trades").delete().eq("id", trade_id).execute()
        
        return 

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An error occurred while deleting the trade: {e}",
        )