# backend/app/apis/v1/chat/router.py
import logging
import uuid
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, UploadFile, File, Form, status
from typing import List, Dict, Any
from supabase import Client

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

# --- Configuration & Logging ---
router = APIRouter()
logger = logging.getLogger(__name__)

# Constants
MAX_FILE_SIZE_MB = 10
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

# --- Session Management ---

@router.get("/sessions", response_model=List[SessionSchema])
def get_sessions(
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client)
):
    """
    Retrieve all chat sessions for the authenticated user.
    """
    try:
        user_id = current_user["sub"]
        res = supabase.table("chat_sessions")\
            .select("id, topic, created_at")\
            .eq("user_id", user_id)\
            .order("created_at", desc=True)\
            .execute()
        return res.data
    except Exception as e:
        logger.error(f"Error fetching sessions for {current_user['sub']}: {e}")
        raise HTTPException(status_code=500, detail="Failed to retrieve sessions")

@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_session(
    session_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client)
):
    """
    Delete a specific chat session.
    """
    try:
        user_id = current_user["sub"]
        # RLS + Explicit check ensures user can only delete their own session
        res = supabase.table("chat_sessions").delete().eq("id", session_id).eq("user_id", user_id).execute()
        
        if not res.data:
            raise HTTPException(status_code=404, detail="Session not found or unauthorized")
        
        logger.info(f"Session {session_id} deleted by {user_id}")
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
    """
    Retrieve message history for a specific session.
    """
    try:
        res = supabase.table("chat_messages")\
            .select("role, encrypted_content, created_at")\
            .eq("session_id", session_id)\
            .order("id", desc=True)\
            .execute()
        
        # Return reversed list (Chronological order for UI)
        return [
            {"role": m["role"], "content": m["encrypted_content"], "created_at": m["created_at"]}
            for m in reversed(res.data) 
        ]
    except Exception as e:
        logger.error(f"Error fetching history for session {session_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to load chat history")

# --- File Operations ---

@router.post("/upload")
async def analyze_file(
    file: UploadFile = File(...),
    message: str = Form(""),
    session_id: str = Form(...),
    supabase: Client = Depends(get_authenticated_client)
):
    """
    Upload and analyze a CSV file for trade importing.
    Includes file size checks and temporary storage.
    """
    try:
        # 1. Security Check: File Size
        file.file.seek(0, 2)
        size = file.file.tell()
        file.file.seek(0)
        
        if size > MAX_FILE_SIZE_BYTES:
            raise HTTPException(status_code=413, detail=f"File too large. Limit is {MAX_FILE_SIZE_MB}MB")

        # 2. Read and Store
        content = await file.read()
        file_ext = file.filename.split('.')[-1]
        if file_ext.lower() != 'csv':
             raise HTTPException(status_code=400, detail="Only CSV files are supported")

        file_path = f"temp/{uuid.uuid4()}.{file_ext}"
        
        # Upload to Supabase Storage (Temp Bucket)
        supabase.storage.from_("temp-imports").upload(file_path, content)
        
        # 3. Analyze Headers
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
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"File analysis failed: {e}")
        raise HTTPException(status_code=400, detail=f"Analysis failed: {str(e)}")

@router.post("/import-confirm")
async def confirm_import(
    payload: Dict[str, Any], 
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client)
):
    """
    Execute the trade import after user confirmation.
    """
    try:
        user_id = current_user["sub"]
        
        # Download file from temp storage
        res = supabase.storage.from_("temp-imports").download(payload["file_path"])
        
        # Process CSV
        trades_data = csv_parser.process_and_normalize(res, payload["mapping"])
        
        # Enrich data
        from datetime import datetime
        for trade in trades_data:
            trade["user_id"] = user_id
            if "entry_time" not in trade:
                trade["entry_time"] = datetime.now().isoformat()
        
        # Batch Insert
        if trades_data:
            supabase.table("trades").insert(trades_data).execute()
        
        # Cleanup
        supabase.storage.from_("temp-imports").remove([payload["file_path"]])
        
        logger.info(f"User {user_id} imported {len(trades_data)} trades")
        return {"status": "success", "count": len(trades_data), "message": f"Successfully imported {len(trades_data)} trades."}
        
    except Exception as e:
        logger.error(f"Import execution failed: {e}")
        raise HTTPException(status_code=500, detail=f"Import failed: {str(e)}")

# --- Main Chat Handler ---

@router.post("", response_model=ChatResponse)
async def chat_with_ai(
    request: ChatRequest,
    background_tasks: BackgroundTasks,
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client)
):
    """
    Core RAG Chat Endpoint.
    Handles:
    1. Session creation
    2. Intent detection (Trade Logging)
    3. RAG Context Retrieval
    4. LLM Generation
    """
    user_id = current_user["sub"]
    session_id = request.session_id
    is_new_session = False

    try:
        # 1. Create Session if needed
        if not session_id:
            is_new_session = True
            initial_topic = (request.message[:45] + "...") if len(request.message) > 45 else request.message
            
            sess_res = supabase.table("chat_sessions").insert({"user_id": user_id, "topic": initial_topic}).execute()
            
            if not sess_res.data: 
                raise HTTPException(500, "Failed to create session")
            
            session_id = sess_res.data[0]["id"]
            
            # Offload title generation to background task
            background_tasks.add_task(generate_session_title, session_id, request.message, supabase)

        # 2. Check for Trade Intent (Agentic Behavior)
        trade_proposal = await parse_trade_intent(request.message)
        
        if trade_proposal:
            logger.info(f"Trade intent detected for session {session_id}")
            
            # Log User Message
            supabase.table("chat_messages").insert({
                "session_id": session_id, "user_id": user_id, "role": "user", "encrypted_content": request.message
            }).execute()

            # Log AI Response
            ai_response_text = "I've drafted this trade based on your message. Please review and confirm."
            supabase.table("chat_messages").insert({
                "session_id": session_id, "user_id": user_id, "role": "assistant", "encrypted_content": ai_response_text
            }).execute()

            return {
                "response": ai_response_text,
                "session_id": session_id,
                "usage": {"total_tokens": 0},
                "tool_call": {
                    "type": "trade-confirmation",
                    "data": trade_proposal
                }
            }

        # 3. Standard RAG Chat Flow
        # Fetch history (limited) and RAG context (recent trades/strategies)
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

        # Call LLM
        ai_result = await llm_client.generate_response(
            messages=messages, 
            model=request.model, 
            provider=request.provider
        )
        
        # Save Messages to DB
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

    except Exception as e:
        logger.error(f"Chat processing failed: {str(e)}", exc_info=True)
        # Clean up empty session if it crashed immediately
        if is_new_session and session_id:
             supabase.table("chat_sessions").delete().eq("id", session_id).execute()
             
        raise HTTPException(status_code=502, detail=f"AI Service Error: {str(e)}")