from fastapi import APIRouter, Depends, HTTPException, status, Body
from typing import List

from ..libs import schemas
from ..libs.supabase_client import get_supabase_client, Client
from ..auth.dependencies import get_current_user
from postgrest.base_request_builder import SingleAPIResponse

# Initialize the router
router = APIRouter()

# Dependency to ensure user is authenticated on all endpoints in this router
AuthUser = Depends(get_current_user)

@router.post(
    "/",
    response_model=schemas.StrategyResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_strategy(
    strategy_data: schemas.StrategyBase,
    current_user: schemas.UserInDB = AuthUser,
    supabase_client: Client = Depends(get_supabase_client),
):
    """
    Creates a new trading strategy (playbook) for the authenticated user.
    """
    # 1. Prepare data with user_id
    strategy_in = strategy_data.model_dump()
    strategy_in['user_id'] = current_user.user_id
    
    try:
        # 2. Insert into the 'strategies' table.
        # RLS (Row Level Security) ensures this user_id insertion is valid.
        response: SingleAPIResponse = supabase_client.table("strategies").insert(strategy_in).execute()
        
        # 3. Handle response and validation
        if not response.data:
             raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Database failed to return the created strategy.",
            )
             
        # The Supabase response data format is a list of dictionaries.
        # We parse the first element into our Pydantic response schema.
        created_strategy = schemas.StrategyResponse(**response.data[0])
        return created_strategy

    except Exception as e:
        # Catch potential database errors (e.g., integrity constraint violation)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An error occurred while creating the strategy: {e}",
        )


@router.get(
    "/",
    response_model=List[schemas.StrategyResponse],
)
async def read_strategies(
    current_user: schemas.UserInDB = AuthUser,
    supabase_client: Client = Depends(get_supabase_client),
):
    """
    Retrieves all trading strategies belonging to the authenticated user.
    RLS is enabled, so we don't need to explicitly filter by user_id here,
    but filtering by user_id in the Python code can improve performance 
    if RLS adds overhead. Relying on RLS is simpler and safer.
    """
    try:
        # 1. Select all columns from 'strategies'
        # The query is automatically filtered by the RLS policy: WHERE user_id = auth.uid()
        response: SingleAPIResponse = supabase_client.table("strategies").select("*").execute()
        
        # 2. Return the list of strategies. Pydantic handles validation.
        return response.data

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An error occurred while fetching strategies: {e}",
        )


@router.get(
    "/{strategy_id}",
    response_model=schemas.StrategyResponse,
)
async def read_strategy(
    strategy_id: str,
    current_user: schemas.UserInDB = AuthUser,
    supabase_client: Client = Depends(get_supabase_client),
):
    """
    Retrieves a single strategy by ID, ensuring ownership via RLS.
    """
    try:
        # 1. Select all columns where ID matches.
        # RLS ensures that if this strategy_id exists, it must belong to the user.
        response: SingleAPIResponse = (
            supabase_client.table("strategies")
            .select("*")
            .eq("id", strategy_id)
            .single() # Expects exactly one or zero rows
            .execute()
        )
        
        # 2. Check if resource was found
        if not response.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Strategy not found or you do not have permission to access it.",
            )
            
        return response.data

    except Exception as e:
        # The .single() method throws an exception on not found/multiple, which we handle above
        if hasattr(e, 'message') and 'not found' in e.message:
             raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Strategy not found or you do not have permission to access it.",
            )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An error occurred while fetching the strategy: {e}",
        )


@router.put(
    "/{strategy_id}",
    response_model=schemas.StrategyResponse,
)
async def update_strategy(
    strategy_id: str,
    strategy_data: schemas.StrategyBase,
    current_user: schemas.UserInDB = AuthUser,
    supabase_client: Client = Depends(get_supabase_client),
):
    """
    Updates an existing strategy, enforcing ownership and RLS.
    """
    # 1. Prepare data (only updateable fields are passed)
    update_data = strategy_data.model_dump(exclude_unset=True)
    
    try:
        # 2. Update the record where ID matches
        # RLS ensures that only the owner can modify this record.
        response: SingleAPIResponse = (
            supabase_client.table("strategies")
            .update(update_data)
            .eq("id", strategy_id)
            .execute()
        )

        if not response.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Strategy not found or you do not have permission to update it.",
            )
            
        return response.data[0]

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An error occurred while updating the strategy: {e}",
        )


@router.delete(
    "/{strategy_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_strategy(
    strategy_id: str,
    current_user: schemas.UserInDB = AuthUser,
    supabase_client: Client = Depends(get_supabase_client),
):
    """
    Deletes a strategy, enforcing ownership and RLS.
    """
    try:
        # 1. Delete the record where ID matches
        # RLS ensures that only the owner can delete this record.
        # We execute and check if any rows were affected (optional but good practice)
        supabase_client.table("strategies").delete().eq("id", strategy_id).execute()
        
        # Note: Supabase postgrest-py doesn't easily return rows_affected on DELETE
        # without a complex query, so we rely on RLS/404 handling if a subsequent GET 
        # would be done, or we simply return 204 if the query ran without a 500 error.
        return 

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An error occurred while deleting the strategy: {e}",
        )