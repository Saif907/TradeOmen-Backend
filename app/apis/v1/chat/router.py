import logging
import asyncio
from typing import Dict, Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from starlette.concurrency import run_in_threadpool
from supabase import Client

from app.auth.dependency import get_current_user
from app.auth.permissions import validate_ai_usage_limits
from app.services.quota_manager import QuotaManager
from app.services.chat_pipeline import ChatPipeline
from app.lib.data_sanitizer import sanitizer

from app.schemas.chat_schemas import (
    ChatRequest,
    ChatResponse,
    SessionSchema,
    MessageSchema,
    ChatUsage,
)
from app.apis.v1.chat.dependencies import get_authenticated_client

logger = logging.getLogger("tradeomen.chat")
router = APIRouter()


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------
def extract_user_id(user: Dict[str, Any]) -> str:
    uid = user.get("id") or user.get("sub") or user.get("user_id")
    if not uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return str(uid)


async def supabase_exec(fn):
    """Run blocking Supabase SDK calls safely."""
    return await run_in_threadpool(fn)


async def _record_usage_background(user_id: str, profile: Dict[str, Any], tokens: int):
    """
    Updates usage stats in the background without blocking the response.
    """
    try:
        # 1. Increment Chat Count
        await QuotaManager.increment_daily_chat(user_id, profile)
        
        # 2. Record Token Usage (Lazy reservation)
        # Note: If they hit the limit exactly during this generation, this might fail,
        # but that's acceptable for post-generation accounting.
        await QuotaManager.reserve_ai_tokens(user_id, profile, tokens)
    except Exception as e:
        # Log but don't crash the request
        logger.error(f"Background usage update failed for user {user_id}: {e}")


# ---------------------------------------------------------------------
# 1. Sessions
# ---------------------------------------------------------------------
@router.get("/sessions", response_model=List[SessionSchema])
async def get_sessions(
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client),
):
    user_id = extract_user_id(current_user)

    try:
        res = await supabase_exec(
            lambda: supabase.table("chat_sessions")
            .select("id, topic, created_at")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .execute()
        )
        return res.data or []
    except Exception:
        logger.exception("Failed to fetch sessions")
        raise HTTPException(500, "Failed to retrieve sessions")


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client),
):
    user_id = extract_user_id(current_user)

    res = await supabase_exec(
        lambda: supabase.table("chat_sessions")
        .delete()
        .eq("id", session_id)
        .eq("user_id", user_id)
        .execute()
    )

    if not res.data:
        raise HTTPException(404, "Session not found")


@router.get("/{session_id}/messages", response_model=List[MessageSchema])
async def get_session_messages(
    session_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client),
):
    user_id = extract_user_id(current_user)

    try:
        res = await supabase_exec(
            lambda: supabase.table("chat_messages")
            .select("role, content, created_at")
            .eq("session_id", session_id)
            .eq("user_id", user_id)
            .order("created_at")
            .execute()
        )

        return res.data or []
    except Exception:
        logger.exception("Failed to load messages")
        raise HTTPException(500, "Failed to load messages")


# ---------------------------------------------------------------------
# 2. Main Chat Endpoint
# ---------------------------------------------------------------------
@router.post("", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    # FIX: Use the correct permission dependency we defined in permissions.py
    user_profile: Dict[str, Any] = Depends(validate_ai_usage_limits),
    supabase: Client = Depends(get_authenticated_client),
):
    user_id = extract_user_id(user_profile)

    raw_message = (request.message or "").strip()
    if not raw_message:
        raise HTTPException(400, "Message cannot be empty")

    # ✅ PII-only sanitization (NO encryption)
    message = sanitizer.sanitize(raw_message)

    session_id: Optional[str] = request.session_id

    # -------------------------------------------------
    # A. Create session if needed
    # -------------------------------------------------
    if not session_id:
        topic = raw_message[:40] + ("..." if len(raw_message) > 40 else "")
        res = await supabase_exec(
            lambda: supabase.table("chat_sessions")
            .insert({"user_id": user_id, "topic": topic})
            .execute()
        )

        if not res.data:
            raise HTTPException(500, "Failed to create session")

        session_id = res.data[0]["id"]

    session_id = str(session_id)

    # -------------------------------------------------
    # B. Load recent context (last 4 messages)
    # -------------------------------------------------
    try:
        hist = await supabase_exec(
            lambda: supabase.table("chat_messages")
            .select("role, content")
            .eq("session_id", session_id)
            .order("created_at", desc=True)
            .limit(4)
            .execute()
        )
        history = list(reversed(hist.data or []))
    except Exception:
        history = []

    # -------------------------------------------------
    # C. Store user message (sanitized)
    # -------------------------------------------------
    await supabase_exec(
        lambda: supabase.table("chat_messages").insert({
            "session_id": session_id,
            "user_id": user_id,
            "role": "user",
            "content": message,
        }).execute()
    )

    # -------------------------------------------------
    # D. Run Chat Pipeline
    # -------------------------------------------------
    try:
        response_text = await ChatPipeline.process(user_id, message, history)
    except Exception:
        logger.exception("Chat pipeline failure")
        response_text = "I encountered an error processing your request. Please try again."

    sanitized_response = sanitizer.sanitize(response_text)

    # -------------------------------------------------
    # E. Store assistant response
    # -------------------------------------------------
    await supabase_exec(
        lambda: supabase.table("chat_messages").insert({
            "session_id": session_id,
            "user_id": user_id,
            "role": "assistant",
            "content": sanitized_response,
        }).execute()
    )

    # -------------------------------------------------
    # F. Usage tracking (async, non-blocking)
    # -------------------------------------------------
    # FIX: Calculate tokens and update using the correct QuotaManager methods
    est_tokens = max(1, int((len(message) + len(response_text)) / 4))

    asyncio.create_task(
        _record_usage_background(user_id, user_profile, est_tokens)
    )

    return {
        "response": response_text,
        "session_id": session_id,
        "usage": ChatUsage(total_tokens=est_tokens, prompt_tokens=0, completion_tokens=0),
    }