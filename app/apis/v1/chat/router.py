# backend/app/apis/v1/chat/router.py
import logging
from typing import Dict, Any, List

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, status
from supabase import Client

from app.auth.dependency import get_current_user
from app.auth.permissions import check_ai_quota
from app.services.quota_manager import QuotaManager
from app.services.chat_pipeline import ChatPipeline

from app.schemas.chat_schemas import (
    ChatRequest, 
    ChatResponse, 
    SessionSchema, 
    MessageSchema,
    ChatUsage
)
from app.apis.v1.chat.dependencies import get_authenticated_client

logger = logging.getLogger(__name__)
router = APIRouter()

# ---------------------------------------------------------------------
# 1. SESSION MANAGEMENT (CRUD)
# ---------------------------------------------------------------------

@router.get("/sessions", response_model=List[SessionSchema])
def get_sessions(
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client)
):
    try:
        # Convert UUID to string just in case
        user_id = str(current_user["sub"])
        res = supabase.table("chat_sessions") \
            .select("id, topic, created_at") \
            .eq("user_id", user_id) \
            .order("created_at", desc=True) \
            .execute()
        return res.data
    except Exception as e:
        logger.error(f"Error fetching sessions: {e}")
        raise HTTPException(status_code=500, detail="Failed to retrieve sessions")


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_session(
    session_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client)
):
    try:
        user_id = str(current_user["sub"])
        res = supabase.table("chat_sessions").delete() \
            .eq("id", session_id).eq("user_id", user_id).execute()
        
        if not res.data:
            raise HTTPException(status_code=404, detail="Session not found")
        return None
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting session {session_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete session")


@router.get("/{session_id}/messages", response_model=List[MessageSchema])
def get_session_history(
    session_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client)
):
    try:
        res = supabase.table("chat_messages") \
            .select("role, encrypted_content, created_at") \
            .eq("session_id", session_id) \
            .order("created_at") \
            .execute()

        return [
            {
                "role": m["role"],
                "content": m["encrypted_content"],
                "created_at": m["created_at"]
            } 
            for m in res.data or []
        ]
    except Exception as e:
        logger.error(f"Error fetching history: {e}")
        raise HTTPException(status_code=500, detail="Failed to load history")


# ---------------------------------------------------------------------
# 2. MAIN CHAT ENGINE (Fixed UUID Serialization)
# ---------------------------------------------------------------------

@router.post("", response_model=ChatResponse)
async def chat_with_ai(
    request: ChatRequest,
    background_tasks: BackgroundTasks,
    user_profile: Dict[str, Any] = Depends(check_ai_quota),
    supabase: Client = Depends(get_authenticated_client)
):
    """
    The Single Entry Point for the Hybrid Chat System.
    """
    # ✅ FIX: Explicitly cast UUID to string for Supabase (JSON) compatibility
    user_id = str(user_profile["id"])
    
    session_id = request.session_id
    message = request.message.strip()

    if not message:
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    # --- A. Session Init ---
    if not session_id:
        topic = message[:40] + "..."
        # Pass user_id as string
        sess_res = supabase.table("chat_sessions") \
            .insert({"user_id": user_id, "topic": topic}) \
            .execute()
        
        if sess_res.data:
            session_id = sess_res.data[0]["id"]
        else:
            raise HTTPException(status_code=500, detail="Failed to initialize session")
    else:
        # Ensure incoming session_id is also a string (Pydantic usually handles this, but safe to cast)
        session_id = str(session_id)

    # --- B. Fetch Optimized Context ---
    try:
        hist_res = supabase.table("chat_messages") \
            .select("role, encrypted_content") \
            .eq("session_id", session_id) \
            .order("created_at", desc=True) \
            .limit(4) \
            .execute()
        
        history = [
            {"role": r["role"], "content": r["encrypted_content"]} 
            for r in reversed(hist_res.data or [])
        ]
    except Exception:
        history = []

    # --- C. Save User Message ---
    # user_id and session_id are now guaranteed to be strings
    supabase.table("chat_messages").insert({
        "session_id": session_id,
        "user_id": user_id,
        "role": "user",
        "encrypted_content": message
    }).execute()

    # --- D. PROCESS (The Brain) ---
    try:
        # Pass user_id as string to the pipeline too
        response_text = await ChatPipeline.process(user_id, message, history)
    except Exception as e:
        logger.exception(f"Pipeline Critical Failure: {e}")
        response_text = "I encountered a critical system error. Please try again later."

    # --- E. Save AI Response ---
    supabase.table("chat_messages").insert({
        "session_id": session_id,
        "user_id": user_id,
        "role": "assistant",
        "encrypted_content": response_text
    }).execute()

    # --- F. Background Metrics ---
    total_chars = len(message) + len(response_text)
    est_tokens = int(total_chars / 4)

    background_tasks.add_task(
        QuotaManager.increment_usage,
        user_id=user_id,
        metric_type="chat_message",
        extra_data={
            "tokens": est_tokens,
            "needs_reset": user_profile.get("_needs_daily_reset", False)
        }
    )

    return {
        "response": response_text,
        "session_id": session_id,
        "usage": ChatUsage(total_tokens=est_tokens)
    }