# backend/app/apis/v1/chat/router.py
import logging
import uuid
import json
from decimal import Decimal
from datetime import datetime
from typing import List, Dict, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, UploadFile, File, Form, status
from supabase import Client

from app.lib.llm_client import llm_client
from app.auth.dependency import get_current_user
from app.lib.csv_parser import csv_parser 
from app.apis.v1.trades import TradeCreate 
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

# --- Helper: DB Serialization ---
def serialize_for_db(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Prepares a dictionary for Supabase/Postgres insertion.
    - Converts Decimal -> String (to preserve precision in NUMERIC columns)
    - Converts DateTime -> ISO String
    """
    clean = {}
    for k, v in data.items():
        if isinstance(v, Decimal):
            # Pass as string to ensure Postgres NUMERIC parses it exactly
            clean[k] = str(v)
        elif isinstance(v, datetime):
            clean[k] = v.isoformat()
        else:
            clean[k] = v
    return clean

# --- Session Management ---

@router.get("/sessions", response_model=List[SessionSchema])
def get_sessions(
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client)
):
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
    try:
        user_id = current_user["sub"]
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
    try:
        res = supabase.table("chat_messages")\
            .select("role, encrypted_content, created_at")\
            .eq("session_id", session_id)\
            .order("id", desc=True)\
            .execute()
        
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
    message: Optional[str] = Form(None),
    session_id: Optional[str] = Form(None),
    supabase: Client = Depends(get_authenticated_client)
):
    """
    Upload and analyze a CSV file for trade importing.
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
        file_ext = file.filename.split('.')[-1] if '.' in file.filename else ''
        
        if file_ext.lower() != 'csv':
             raise HTTPException(status_code=400, detail="Only CSV files are supported")

        file_path = f"temp/{uuid.uuid4()}.{file_ext}"
        
        # Upload to Supabase Storage
        supabase.storage.from_("temp-imports").upload(file_path, content)
        
        # 3. Analyze Structure (Headers + Sample)
        structure = csv_parser.analyze_structure(content)
        
        # 4. Generate Mapping via LLM
        mapping_proposal = await csv_parser.guess_mapping(
            headers=structure["headers"], 
            sample_row=structure["sample"], 
            user_prompt=message or ""
        )
        
        return {
            "type": "import-confirmation",
            "file_path": file_path,
            "filename": file.filename,
            "mapping": mapping_proposal,
            "detected_headers": structure["headers"],
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
    try:
        user_id = current_user["sub"]
        session_id = payload.get("session_id")
        
        # 1. Download file from temp storage
        res = supabase.storage.from_("temp-imports").download(payload["file_path"])
        
        # 2. Process CSV
        raw_trades = csv_parser.process_and_normalize(res, payload["mapping"])
        
        valid_trades = []
        failed_rows = []

        # 3. Validate & Transform
        for i, raw_trade in enumerate(raw_trades):
            try:
                if "entry_time" not in raw_trade or not raw_trade["entry_time"]:
                    raw_trade["entry_time"] = datetime.now()
                
                if "status" not in raw_trade:
                    raw_trade["status"] = "CLOSED" if raw_trade.get("exit_price") else "OPEN"
                
                # TradeCreate (Pydantic) converts Decimals -> floats AND converts "Long" -> "LONG"
                trade_model = TradeCreate(**raw_trade)
                db_row = serialize_for_db(trade_model.dict(exclude={"notes"}))
                
                if trade_model.notes:
                    db_row["encrypted_notes"] = trade_model.notes
                
                db_row["user_id"] = user_id
                
                # --- PnL Calculation ---
                if "pnl" not in db_row or db_row["pnl"] is None:
                    if trade_model.exit_price and trade_model.exit_price > 0:
                        # ✅ FIX: Direction is now UPPERCASE "LONG"
                        mult = 1.0 if trade_model.direction == "LONG" else -1.0
                        pnl = (trade_model.exit_price - trade_model.entry_price) * trade_model.quantity * mult - trade_model.fees
                        db_row["pnl"] = str(pnl) 

                valid_trades.append(db_row)

            except Exception as e:
                logger.warning(f"Row {i} validation failed: {e}")
                failed_rows.append({"row": i + 1, "error": str(e), "data": raw_trade})

        # 4. Batch Insert
        if valid_trades:
            BATCH_SIZE = 100
            for k in range(0, len(valid_trades), BATCH_SIZE):
                batch = valid_trades[k:k + BATCH_SIZE]
                supabase.table("trades").insert(batch).execute()
        
        # 5. Cleanup
        supabase.storage.from_("temp-imports").remove([payload["file_path"]])
        
        # 6. Response Construction
        msg = f"Successfully imported {len(valid_trades)} trades."
        if failed_rows:
            msg += f" {len(failed_rows)} rows failed validation."
        
        # 7. Persistent Chat Log
        try:
            if not session_id:
                topic = f"Import: {len(valid_trades)} trades"
                sess_res = supabase.table("chat_sessions").insert({"user_id": user_id, "topic": topic}).execute()
                if sess_res.data:
                    session_id = sess_res.data[0]["id"]
                    logger.info(f"Created new session {session_id} for import log")

            if session_id:
                log_content = f"✅ **Import Complete**\n{msg}"
                supabase.table("chat_messages").insert({
                    "session_id": session_id,
                    "user_id": user_id,
                    "role": "assistant",
                    "encrypted_content": log_content
                }).execute()
        except Exception as log_e:
            logger.warning(f"Failed to log import success to chat: {log_e}")

        logger.info(f"User {user_id} imported {len(valid_trades)} trades.")
        
        return {
            "status": "success", 
            "count": len(valid_trades), 
            "failures": len(failed_rows),
            "failed_samples": failed_rows[:5], 
            "message": msg,
            "session_id": session_id
        }
        
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
    user_id = current_user["sub"]
    session_id = request.session_id
    is_new_session = False

    try:
        if not session_id:
            is_new_session = True
            initial_topic = (request.message[:45] + "...") if len(request.message) > 45 else request.message
            sess_res = supabase.table("chat_sessions").insert({"user_id": user_id, "topic": initial_topic}).execute()
            if not sess_res.data: raise HTTPException(500, "Failed to create session")
            session_id = sess_res.data[0]["id"]
            background_tasks.add_task(generate_session_title, session_id, request.message, supabase)

        trade_proposal = await parse_trade_intent(request.message)
        
        if trade_proposal:
            logger.info(f"Trade intent detected for session {session_id}")
            supabase.table("chat_messages").insert({
                "session_id": session_id, "user_id": user_id, "role": "user", "encrypted_content": request.message
            }).execute()
            ai_response_text = "I've drafted this trade based on your message. Please review and confirm."
            supabase.table("chat_messages").insert({
                "session_id": session_id, "user_id": user_id, "role": "assistant", "encrypted_content": ai_response_text
            }).execute()

            return {
                "response": ai_response_text,
                "session_id": session_id,
                "usage": {"total_tokens": 0},
                "tool_call": {"type": "trade-confirmation", "data": trade_proposal}
            }

        memory = build_memory_context(session_id, supabase) if not is_new_session else []
        rag_context = build_trading_context(supabase)
        
        system_prompt = f"""You are TradeLM, a professional trading assistant.
        User Context:
        {rag_context}
        CRITICAL INSTRUCTION:
        - If the user asks to log a trade, and you are seeing this prompt, it means the auto-logger FAILED.
        - DO NOT fake a confirmation.
        """

        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(memory)
        messages.append({"role": "user", "content": request.message})

        ai_result = await llm_client.generate_response(
            messages=messages, model=request.model, provider=request.provider
        )
        
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
        if is_new_session and session_id:
             supabase.table("chat_sessions").delete().eq("id", session_id).execute()
        raise HTTPException(status_code=502, detail=f"AI Service Error: {str(e)}")