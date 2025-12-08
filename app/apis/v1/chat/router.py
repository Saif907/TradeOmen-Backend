# backend/app/apis/v1/chat/router.py
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, UploadFile, File, Form
from typing import List, Dict, Any
from supabase import Client
import uuid

from app.lib.llm_client import llm_client
from app.auth.dependency import get_current_user
from app.lib.csv_parser import csv_parser 
from .schemas import ChatRequest, ChatResponse, SessionSchema, MessageSchema
from .dependencies import get_authenticated_client
from .services import (
    generate_session_title, 
    parse_trade_intent, 
    build_memory_context, 
    build_trading_context
)

router = APIRouter()

# --- Session Management ---

@router.get("/sessions", response_model=List[SessionSchema])
def get_sessions(
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client)
):
    res = supabase.table("chat_sessions")\
        .select("id, topic, created_at")\
        .eq("user_id", current_user["sub"])\
        .order("created_at", desc=True)\
        .execute()
    return res.data

@router.delete("/sessions/{session_id}")
def delete_session(
    session_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client)
):
    res = supabase.table("chat_sessions").delete().eq("id", session_id).eq("user_id", current_user["sub"]).execute()
    if not res.data:
        raise HTTPException(404, "Session not found")
    return {"status": "deleted"}

@router.get("/{session_id}/messages", response_model=List[MessageSchema])
def get_session_history(
    session_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client)
):
    res = supabase.table("chat_messages")\
        .select("role, encrypted_content, created_at")\
        .eq("session_id", session_id)\
        .order("id", desc=True)\
        .execute()
    
    return [
        {"role": m["role"], "content": m["encrypted_content"], "created_at": m["created_at"]}
        for m in reversed(res.data) 
    ]

# --- File Operations ---

@router.post("/upload")
async def analyze_file(
    file: UploadFile = File(...),
    message: str = Form(""),
    session_id: str = Form(...),
    supabase: Client = Depends(get_authenticated_client)
):
    try:
        content = await file.read()
        file_ext = file.filename.split('.')[-1]
        file_path = f"temp/{uuid.uuid4()}.{file_ext}"
        
        supabase.storage.from_("temp-imports").upload(file_path, content)
        headers = csv_parser.read_headers(content)
        mapping_proposal = await csv_parser.guess_mapping(headers, message)
        
        return {
            "type": "import-confirmation",
            "file_path": file_path,
            "filename": file.filename,
            "mapping": mapping_proposal,
            "detected_headers": headers,
            "message": f"I've analyzed **{file.filename}**. Please confirm the column mapping below."
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Analysis failed: {str(e)}")

@router.post("/import-confirm")
async def confirm_import(
    payload: Dict[str, Any], 
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client)
):
    try:
        user_id = current_user["sub"]
        res = supabase.storage.from_("temp-imports").download(payload["file_path"])
        trades_data = csv_parser.process_and_normalize(res, payload["mapping"])
        
        for trade in trades_data:
            trade["user_id"] = user_id
            if "entry_time" not in trade:
                from datetime import datetime
                trade["entry_time"] = datetime.now().isoformat()
        
        if trades_data:
            supabase.table("trades").insert(trades_data).execute()
        
        supabase.storage.from_("temp-imports").remove([payload["file_path"]])
        return {"status": "success", "count": len(trades_data), "message": f"Successfully imported {len(trades_data)} trades."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Import failed: {str(e)}")

# --- Main Chat Handler ---

@router.post("", response_model=ChatResponse)
async def chat_with_ai(
    request: ChatRequest,
    background_tasks: BackgroundTasks,
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client)
):
    user_id = current_user["sub"]
    session_id = request.session_id
    is_new_session = False

    # 1. Create Session
    if not session_id:
        is_new_session = True
        initial_topic = (request.message[:45] + "...") if len(request.message) > 45 else request.message
        sess_res = supabase.table("chat_sessions").insert({"user_id": user_id, "topic": initial_topic}).execute()
        if not sess_res.data: raise HTTPException(500, "Failed to create session")
        session_id = sess_res.data[0]["id"]
        # Trigger title generation (using Gemini)
        background_tasks.add_task(generate_session_title, session_id, request.message, supabase)

    # 2. Check for Trade Intent (Agentic Behavior)
    trade_proposal = await parse_trade_intent(request.message)
    if trade_proposal:
        # Log User Message
        supabase.table("chat_messages").insert({
            "session_id": session_id, "user_id": user_id, "role": "user", "encrypted_content": request.message
        }).execute()

        # Log AI Response (Card Prompt)
        ai_response_text = "I've drafted this trade based on your message. Please review and confirm."
        supabase.table("chat_messages").insert({
            "session_id": session_id, "user_id": user_id, "role": "assistant", "encrypted_content": ai_response_text
        }).execute()

        return {
            "response": ai_response_text,
            "session_id": session_id,
            "usage": {"total_tokens": 0},
            # ✅ Returns data to trigger TradeConfirmationCard in frontend
            "tool_call": {
                "type": "trade-confirmation",
                "data": trade_proposal
            }
        }

    # 3. Standard RAG Chat Flow
    memory = build_memory_context(session_id, supabase) if not is_new_session else []
    rag_context = build_trading_context(supabase)
    
    system_prompt = f"""You are TradeLM, a professional trading assistant.
    User Context:
    {rag_context}
    
    CRITICAL INSTRUCTION:
    - If the user asks to log a trade, and you are seeing this prompt, it means the auto-logger FAILED.
    - DO NOT fake a confirmation.
    - Instead say: "I detected you want to log a trade, but I couldn't capture the details automatically. Please try rephrasing."
    """

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(memory)
    messages.append({"role": "user", "content": request.message})

    try:
        # Call LLM
        ai_result = await llm_client.generate_response(
            messages=messages, 
            model=request.model, 
            provider=request.provider
        )
    except Exception as e:
        if is_new_session: supabase.table("chat_sessions").delete().eq("id", session_id).execute()
        raise HTTPException(status_code=502, detail=f"LLM Error: {str(e)}")
    
    # Save Messages
    msgs = [
        {"session_id": session_id, "user_id": user_id, "role": "user", "encrypted_content": request.message},
        {"session_id": session_id, "user_id": user_id, "role": "assistant", "encrypted_content": ai_result["content"]}
    ]
    supabase.table("chat_messages").insert(msgs).execute()

    return {
        "response": ai_result["content"],
        "session_id": session_id,
        "usage": ai_result["usage"]
    }