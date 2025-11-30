from fastapi import APIRouter, Depends, HTTPException, status, Body
from typing import List, Dict, Any
import httpx
from postgrest.base_request_builder import SingleAPIResponse

from ..libs import schemas
from ..libs.supabase_client import get_supabase_client, Client
from ..auth.dependencies import get_current_user, check_plan_access
from ..libs.config import settings

# Initialize the router
router = APIRouter()

# Dependency for authentication
AuthUser = Depends(get_current_user)

# Dependency to check AI Chat Prompt Limit (SaaS Tier Enforcement)
CheckChatLimit = check_plan_access(schemas.PlanFeature.AI_CHAT_PROMPT_LIMIT)

# --- Internal Client Setup for AI Microservice ---
# Use a persistent httpx.AsyncClient for efficient microservice communication.
AI_CLIENT = httpx.AsyncClient(
    base_url=settings.AI_MICROSERVICE_URL,
    # Inject the shared secret key for service-to-service authentication
    headers={"X-Microservice-Auth": settings.AI_SERVICE_SECRET_KEY}
)


# --- 1. Chat Session Management Endpoints ---

@router.post(
    "/sessions",
    response_model=schemas.ChatSessionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_chat_session(
    session_data: schemas.ChatSessionCreate,
    current_user: schemas.UserInDB = AuthUser,
    supabase_client: Client = Depends(get_supabase_client),
):
    """Creates a new chat session/thread for the user."""
    session_in = session_data.model_dump()
    session_in['user_id'] = current_user.user_id
    
    try:
        response: SingleAPIResponse = supabase_client.table("chat_sessions").insert(session_in).execute()
        
        if not response.data:
             raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Database failed to return the created session.",
            )
             
        return schemas.ChatSessionResponse(**response.data[0])

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error creating chat session: {e}",
        )


@router.get(
    "/sessions",
    response_model=List[schemas.ChatSessionResponse],
)
async def get_chat_sessions(
    current_user: schemas.UserInDB = AuthUser,
    supabase_client: Client = Depends(get_supabase_client),
):
    """Retrieves all chat sessions for the authenticated user."""
    try:
        # RLS ensures the results are filtered by user_id
        response: SingleAPIResponse = supabase_client.table("chat_sessions").select("*").execute()
        return response.data

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error fetching chat sessions: {e}",
        )


# --- 2. Chat Message Processing (The AI Core) ---

@router.post(
    "/chat/{session_id}",
    response_model=schemas.ChatMessage, # Response is the AI's message only
    dependencies=[Depends(CheckChatLimit)], # Enforce SaaS prompt limit
)
async def post_chat_message(
    session_id: str,
    message: schemas.ChatMessage,
    current_user: schemas.UserInDB = AuthUser,
    supabase_client: Client = Depends(get_supabase_client),
):
    """
    Forwards user message to the AI Microservice and manages history/usage count.
    """
    
    # Check if the user is authorized to use the session (RLS handles ownership, 
    # but a quick check ensures the session exists for this user)
    session_check_res = supabase_client.table("chat_sessions").select("id").eq("id", session_id).single().execute()
    if not session_check_res.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat session not found.")
    
    # 1. Fetch chat history to provide context to the AI
    history_res = supabase_client.table("chat_messages").select("role, content").eq("session_id", session_id).order("created_at", desc=False).execute()
    
    history_messages = [schemas.ChatMessage(**m) for m in history_res.data]
    
    # 2. Add the new user message to the local list
    history_messages.append(message)
    
    # 3. Prepare the Anonymized Request for the AI Microservice
    # NOTE: We DO NOT send the user's email or sensitive PII.
    ai_payload = {
        "user_id": current_user.user_id, # Non-PII identifier for tool calls/data fetching
        "user_plan": current_user.plan.value,
        "history": [m.model_dump() for m in history_messages],
        "new_message": message.model_dump()
    }

    try:
        # 4. Call the AI Microservice (The Network Hop)
        ai_response = await AI_CLIENT.post(
            f"/chat/{session_id}", 
            json=ai_payload,
            timeout=30.0 # Longer timeout for LLM generation
        )
        
        # 5. Handle AI Service Errors
        if ai_response.status_code != status.HTTP_200_OK:
            error_detail = ai_response.json().get("detail", "Unknown AI service error")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"AI Microservice failed: {error_detail}",
            )
            
        # 6. Process AI Response
        ai_message_data = ai_response.json()
        ai_message = schemas.ChatMessage(**ai_message_data)

        # 7. Persist Messages (User message first, then AI message) and update usage count
        
        # A. Save User Message
        user_message_in_db = message.model_dump()
        user_message_in_db.update({'session_id': session_id, 'user_id': current_user.user_id})
        supabase_client.table("chat_messages").insert(user_message_in_db).execute()

        # B. Save AI Message
        ai_message_in_db = ai_message.model_dump()
        ai_message_in_db.update({'session_id': session_id, 'user_id': current_user.user_id})
        supabase_client.table("chat_messages").insert(ai_message_in_db).execute()
        
        # C. Increment Prompt Count (for SaaS Tier Enforcement)
        supabase_client.rpc('increment_chat_count', {'session_id': session_id}).execute()
        
        # 8. Return the AI's message to the frontend
        return ai_message

    except httpx.HTTPError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Cannot connect to AI Microservice at {settings.AI_MICROSERVICE_URL}",
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An unexpected error occurred: {e}",
        )