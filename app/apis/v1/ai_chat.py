# backend/app/apis/v1/ai_chat.py
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
from supabase import create_client, Client

from app.core.config import settings
from app.lib.llm_client import llm_client

router = APIRouter()
security = HTTPBearer()

# --- Schemas ---

class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    model: str = "gpt-4-turbo"
    provider: str = "openai"

class ChatResponse(BaseModel):
    response: str
    session_id: str
    usage: Dict[str, int]

# --- Dependency: Authenticated Supabase Client ---
# (Reused pattern from trades.py to ensure RLS compliance)
def get_authenticated_client(creds: HTTPAuthorizationCredentials = Depends(security)) -> Client:
    token = creds.credentials
    client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)
    client.postgrest.auth(token)
    return client

# --- Helper: Context Builder (REST Version) ---

def build_trading_context(supabase: Client) -> str:
    """
    Fetches recent trades and strategies via Supabase REST API.
    RLS ensures we only get the requesting user's data.
    """
    try:
        # 1. Fetch last 5 trades
        trades_res = supabase.table("trades")\
            .select("symbol, direction, entry_price, pnl, status, entry_date")\
            .order("entry_date", desc=True)\
            .limit(5)\
            .execute()
        
        # 2. Fetch active strategies
        strategies_res = supabase.table("strategies")\
            .select("name, description")\
            .execute()
        
        # Format as string for the LLM
        context_str = "Recent User Activity:\n"
        if trades_res.data:
            for t in trades_res.data:
                context_str += f"- {t['symbol']} ({t['direction']}): PnL ${t.get('pnl', 'N/A')}, Status: {t['status']}\n"
        else:
            context_str += "- No recent trades found.\n"
            
        context_str += "\nUser Strategies:\n"
        if strategies_res.data:
            for s in strategies_res.data:
                context_str += f"- {s['name']}: {s['description']}\n"
        else:
            context_str += "- No active strategies.\n"
            
        return context_str
    except Exception as e:
        # Fail gracefully if DB is down, don't crash the chat
        return f"Error loading context: {str(e)}"

# --- Endpoints ---

@router.post("", response_model=ChatResponse)
async def chat_with_ai(
    request: ChatRequest,
    supabase: Client = Depends(get_authenticated_client)
):
    """
    Orchestrates the AI conversation:
    1. Gets User ID from Auth.
    2. Creates/Retrieves Session.
    3. Fetches RAG Context.
    4. Calls LLM (Async).
    5. Saves History.
    """
    # Get User ID securely from the auth context
    user_resp = supabase.auth.get_user()
    if not user_resp.user:
        raise HTTPException(status_code=401, detail="User not authenticated")
    user_id = user_resp.user.id

    # 1. Session Management
    session_id = request.session_id
    if not session_id:
        # Create new session
        topic = (request.message[:47] + "...") if len(request.message) > 50 else request.message
        session_data = {
            "user_id": user_id,
            "topic": topic
        }
        sess_res = supabase.table("chat_sessions").insert(session_data).execute()
        if sess_res.data:
            session_id = sess_res.data[0]["id"]
        else:
            raise HTTPException(status_code=500, detail="Failed to create chat session")

    # 2. Build Context (RAG)
    system_context = build_trading_context(supabase)
    system_prompt = f"""
    You are TradeLM, an elite AI trading coach.
    
    User Context:
    {system_context}
    
    Your Goal:
    Analyze the user's input in the context of their recent trading activity.
    Be concise, professional, and data-driven.
    """

    # 3. Call LLM (Async)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": request.message}
    ]
    
    ai_result = await llm_client.generate_response(
        messages=messages,
        model=request.model,
        provider=request.provider
    )
    
    # 4. Save Interaction to DB
    # Note: 'encrypted_content' suggests encryption. 
    # For MVP, we save plain text unless app/lib/encryption.py is integrated.
    total_tokens = ai_result["usage"].get("total_tokens", 0)
    
    messages_payload = [
        {
            "session_id": session_id,
            "user_id": user_id,
            "role": "user",
            "encrypted_content": request.message, 
            "model_name": request.model,
            "usage_tokens": 0
        },
        {
            "session_id": session_id,
            "user_id": user_id,
            "role": "assistant",
            "encrypted_content": ai_result["content"],
            "model_name": request.model,
            "usage_tokens": total_tokens
        }
    ]
    
    supabase.table("chat_messages").insert(messages_payload).execute()

    return {
        "response": ai_result["content"],
        "session_id": str(session_id),
        "usage": ai_result["usage"]
    }

@router.get("/history", response_model=List[Dict[str, Any]])
def get_chat_history(
    session_id: str,
    supabase: Client = Depends(get_authenticated_client)
):
    """
    Retrieve message history for a specific session.
    RLS prevents accessing sessions that don't belong to you.
    """
    res = supabase.table("chat_messages")\
        .select("role, encrypted_content, created_at")\
        .eq("session_id", session_id)\
        .order("created_at", desc=False)\
        .execute()
        
    # Map for frontend
    return [
        {
            "role": m["role"], 
            "content": m["encrypted_content"], 
            "created_at": m["created_at"]
        } 
        for m in res.data
    ]