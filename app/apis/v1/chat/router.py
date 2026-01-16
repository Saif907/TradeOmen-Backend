import logging
import asyncio
import uuid
import json
from typing import Dict, Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form
from starlette.concurrency import run_in_threadpool
from supabase import Client

from app.auth.dependency import get_current_user
from app.auth.permissions import validate_ai_usage_limits, check_import_quota
from app.services.quota_manager import QuotaManager
from app.services.chat_pipeline import ChatPipeline
from app.lib.data_sanitizer import sanitizer
from app.lib.csv_parser import csv_parser
from app.apis.v1.chat.dependencies import get_authenticated_client

from app.schemas.chat_schemas import (
    ChatRequest,
    ChatResponse,
    SessionSchema,
    MessageSchema,
    ChatUsage,
    SessionUpdate,      
    UploadResponse,     
    ImportConfirmSchema 
)

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
    """Run blocking Supabase SDK calls safely in a threadpool."""
    return await run_in_threadpool(fn)


async def _record_usage_background(user_id: str, profile: Dict[str, Any], tokens: int):
    """
    Updates usage stats in the background without blocking the response.
    """
    try:
        await QuotaManager.increment_daily_chat(user_id)
        await QuotaManager.reserve_ai_tokens(user_id, profile, tokens)
    except Exception as e:
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


@router.patch("/sessions/{session_id}", response_model=SessionSchema)
async def rename_session(
    session_id: str,
    payload: SessionUpdate,
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client),
):
    user_id = extract_user_id(current_user)
    res = await supabase_exec(
        lambda: supabase.table("chat_sessions")
        .update({"topic": payload.topic})
        .eq("id", session_id)
        .eq("user_id", user_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(404, "Session not found")
    return res.data[0]


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
# 2. File Upload & Import
# ---------------------------------------------------------------------

@router.post("/upload", response_model=UploadResponse)
async def upload_chat_file(
    file: UploadFile = File(...),
    session_id: str = Form(...),
    message: str = Form(""),
    current_user: Dict[str, Any] = Depends(check_import_quota),
):
    """
    Analyzes a CSV file structure and returns a preview/mapping suggestion.
    """
    if not file.filename.endswith(".csv"):
        raise HTTPException(400, "Only CSV files are supported")
    
    try:
        content = await file.read()
        
        # Analyze structure
        analysis = csv_parser.analyze_structure(content)
        
        # Heuristic mapping (fast, no LLM cost)
        mapping = csv_parser._heuristic_mapping(analysis["headers"], analysis["sample"])
        
        # TODO: In production, upload 'content' to Supabase Storage (bucket: 'temp-imports')
        # returning the storage path instead of a placeholder.
        temp_path = f"temp/{uuid.uuid4()}/{file.filename}"

        return {
            "status": "success",
            "file_path": temp_path,
            "filename": file.filename,
            "detected_headers": analysis["headers"],
            "preview": analysis["preview"],
            "mapping": mapping,
            "message": "File analyzed. Please confirm column mapping."
        }
    except Exception as e:
        logger.error(f"File upload analysis failed: {e}")
        raise HTTPException(500, "Failed to analyze file")


@router.post("/import-confirm")
async def confirm_import(
    payload: ImportConfirmSchema,
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client),
):
    """
    Placeholder: Actually processes the CSV and inserts trades.
    Requires downloading file from Storage based on payload.file_path.
    """
    # 1. Download file from Storage using payload.file_path
    # 2. Parse using payload.mapping
    # 3. Insert trades
    return {"status": "success", "count": 0}


# ---------------------------------------------------------------------
# 3. Main Chat Endpoint
# ---------------------------------------------------------------------
@router.post("", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    user_profile: Dict[str, Any] = Depends(validate_ai_usage_limits),
    supabase: Client = Depends(get_authenticated_client),
):
    user_id = extract_user_id(user_profile)
    raw_message = (request.message or "").strip()
    if not raw_message:
        raise HTTPException(400, "Message cannot be empty")

    message = sanitizer.sanitize(raw_message)
    session_id = request.session_id

    # A. Create session if needed
    if not session_id:
        topic = raw_message[:40] + ("..." if len(raw_message) > 40 else "")
        try:
            res = await supabase_exec(
                lambda: supabase.table("chat_sessions")
                .insert({"user_id": user_id, "topic": topic})
                .execute()
            )
            if not res.data:
                raise Exception("No data returned")
            session_id = str(res.data[0]["id"])
        except Exception as e:
            logger.error(f"Failed to create session: {e}")
            raise HTTPException(500, "Failed to create chat session")

    # B. Load History (last 4 messages)
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

    # C. Store User Message (Plaintext)
    await supabase_exec(
        lambda: supabase.table("chat_messages").insert({
            "session_id": session_id,
            "user_id": user_id,
            "role": "user",
            "content": message,
        }).execute()
    )

    # D. Process AI Response
    try:
        # Pass web_search flag implicitly if needed by pipeline logic
        # web_search = getattr(request, "web_search", False)
        response_text = await ChatPipeline.process(user_id, message, history)
    except Exception:
        logger.exception("Chat pipeline failure")
        response_text = "I encountered an error processing your request. Please try again."

    sanitized_response = sanitizer.sanitize(response_text)

    # E. Store Assistant Response (Plaintext)
    await supabase_exec(
        lambda: supabase.table("chat_messages").insert({
            "session_id": session_id,
            "user_id": user_id,
            "role": "assistant",
            "content": sanitized_response,
        }).execute()
    )

    # F. Usage Tracking (Async)
    est_tokens = max(1, int((len(message) + len(response_text)) / 4))
    asyncio.create_task(_record_usage_background(user_id, user_profile, est_tokens))

    return {
        "response": response_text,
        "session_id": session_id,
        "usage": ChatUsage(total_tokens=est_tokens, prompt_tokens=0, completion_tokens=0),
    }