# backend/app/apis/v1/chat/router.py
"""
Chat router - production-ready adjustments.

Key points:
- Integrated QuotaManager for usage limits (chats, imports, web search).
- chat_messages.encrypted_content is treated as PLAINTEXT here (historical column name).
- Sensitive trade fields (trades.encrypted_notes, screenshots) remain encrypted elsewhere.
- LLM context uses the last 10 messages.
- Conversation summarizer code remains present but is DISABLED by default (SUMMARY_ENABLED = False).
- Perplexity web search continues to be supported.
"""

import logging
import json
from decimal import Decimal
from datetime import datetime
from typing import List, Dict, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, status
from supabase import Client

# --- Core Dependencies ---
from app.core.database import db
from app.auth.dependency import get_current_user
from app.lib.llm_client import llm_client
from app.lib.csv_parser import csv_parser
from app.apis.v1.trades import TradeCreate

# --- Schemas & Services ---
from .schemas import ChatRequest, ChatResponse, SessionSchema, MessageSchema
from .dependencies import get_authenticated_client
from .services import generate_session_title, parse_trade_intent
from app.services.chat_pipeline import process_chat_message
from app.services.quota_manager import QuotaManager
from app.auth.permissions import check_ai_quota

logger = logging.getLogger(__name__)
router = APIRouter()

# -------------------------------------------------------------------
# CONFIG
# -------------------------------------------------------------------
MAX_FILE_SIZE_MB = 10
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

# Use last 10 messages as LLM context
RECENT_CONTEXT_LIMIT = 10

# Conversation summary generation toggled OFF to conserve Gemini quota.
SUMMARY_ENABLED = False
SUMMARY_UPDATE_THRESHOLD = 12  # only used when SUMMARY_ENABLED is True

# Perplexity model default (ensure this exists in your env or change it)
PERPLEXITY_MODEL_DEFAULT = "sonar"


# -------------------------------------------------------------------
# HELPERS
# -------------------------------------------------------------------
async def get_full_user_profile(user_id: str) -> Dict[str, Any]:
    """
    Helper to fetch profile specifically for quota checks.
    Includes last_chat_reset_at needed for daily resets.
    """
    if not db.pool:
        # Fallback if DB isn't ready, defaults to FREE limits implicitly
        return {"plan_tier": "FREE"}
    
    query = """
        SELECT id, plan_tier, daily_chat_count, last_chat_reset_at, 
               monthly_import_count, monthly_ai_tokens_used 
        FROM public.user_profiles 
        WHERE id = $1
    """
    row = await db.fetch_one(query, user_id)
    return dict(row) if row else {"plan_tier": "FREE"}


def estimate_tokens(text: str) -> int:
    """Rough estimation (1 token ~= 4 chars) for usage tracking."""
    if not text:
        return 0
    return len(text) // 4


def serialize_for_db(data: Dict[str, Any]) -> Dict[str, Any]:
    """Prepare dictionary for insertion into Postgres via Supabase client."""
    clean: Dict[str, Any] = {}
    for k, v in data.items():
        if isinstance(v, Decimal):
            clean[k] = str(v)
        elif isinstance(v, datetime):
            clean[k] = v.isoformat()
        else:
            clean[k] = v
    return clean


def is_session_memory_query(text: str) -> bool:
    """Detect quick session-memory queries that don't need an LLM."""
    if not text:
        return False
    t = text.lower()
    return any(p in t for p in (
        "what did i ask",
        "what was my last",
        "repeat my",
        "previous message",
        "last message",
        "what did we talk about",
        "what did i just ask"
    ))


def is_trade_proposal_trigger(text: str) -> bool:
    """Conservative trigger to draft a trade. Avoid false positives."""
    if not text:
        return False
    t = text.lower().strip()
    starters = ("buy ", "sell ", "long ", "short ", "enter ", "exit ")
    if t.startswith(starters):
        return True
    for phrase in ("draft trade", "create trade", "plan trade", "i want to buy", "i want to sell"):
        if phrase in t:
            return True
    return False


def fetch_recent_messages(supabase: Client, session_id: str, limit: int = RECENT_CONTEXT_LIMIT) -> List[Dict[str, str]]:
    """
    Fetch last N user+assistant messages (old → new).
    Messages are returned as plaintext from the `encrypted_content` column.
    Returns: list of {"role":"user"|"assistant", "content": "..."}.
    """
    try:
        res = supabase.table("chat_messages") \
            .select("role, encrypted_content, created_at") \
            .eq("session_id", session_id) \
            .order("created_at", desc=True) \
            .limit(limit) \
            .execute()
    except Exception as e:
        logger.warning("Failed to fetch recent messages for context: %s", e)
        return []

    msgs: List[Dict[str, str]] = []
    for row in reversed(res.data or []):
        msgs.append({
            "role": row.get("role", "user"),
            "content": row.get("encrypted_content", "")  # plaintext by design
        })
    return msgs


async def run_web_search_with_perplexity(message: str, conversation_context: List[Dict[str, str]]) -> str:
    """
    Route web-search queries to Perplexity via llm_client.
    Returns LLM response content string.
    """
    system_instruction = (
        "You are a market research assistant. Use live web information to produce a concise market snapshot. "
        "Cite sources when relevant. Use neutral, factual tone and do not invent facts."
    )

    messages: List[Dict[str, str]] = [{"role": "system", "content": system_instruction}]
    for m in (conversation_context or [])[-4:]:
        # pass the last few turns to provide context
        messages.append({"role": m.get("role", "user"), "content": m.get("content", "")})
    messages.append({"role": "user", "content": message})

    try:
        resp = await llm_client.generate_response(
            messages=messages,
            provider="perplexity",
            model=PERPLEXITY_MODEL_DEFAULT,
            temperature=0.2,
            max_tokens=800
        )
        return resp.get("content", "")
    except Exception as e:
        logger.warning("Perplexity web search call failed: %s", e)
        raise


async def update_conversation_summary_background(supabase: Client, session_id: str, recent_context: List[Dict[str, str]]):
    """
    Generate a short conversation summary and persist it. Scheduling controlled by SUMMARY_ENABLED.
    """
    try:
        prompt = (
            "Summarize the conversation below in 3 short bullet points. "
            "Focus on user goals, strategies discussed, and decisions. Do NOT include raw trade numbers.\n\n"
            f"{json.dumps(recent_context, indent=2)}"
        )
        messages = [
            {"role": "system", "content": "You are a conversation summarizer for a trading journal."},
            {"role": "user", "content": prompt}
        ]
        resp = await llm_client.generate_response(
            messages=messages,
            provider="gemini",
            model="gemini-2.5-flash",
            temperature=0.0,
            max_tokens=300
        )
        summary = resp.get("content", "").strip()
        if summary:
            supabase.table("chat_sessions").update({"conversation_summary": summary}).eq("id", session_id).execute()
    except Exception as e:
        logger.warning("Failed to update conversation summary for %s: %s", session_id, e)


# ----------------------
# Session routes
# ----------------------
@router.get("/sessions", response_model=List[SessionSchema])
def get_sessions(
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client)
):
    try:
        user_id = current_user["sub"]
        res = supabase.table("chat_sessions") \
            .select("id, topic, created_at") \
            .eq("user_id", user_id) \
            .order("created_at", desc=True) \
            .execute()
        return res.data
    except Exception as e:
        logger.error("Error fetching sessions for %s: %s", current_user.get("sub"), e)
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
        logger.info("Session %s deleted by %s", session_id, user_id)
        return None
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error deleting session %s: %s", session_id, e)
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

        messages = []
        for m in res.data or []:
            # return plaintext 'encrypted_content' for compatibility
            messages.append({
                "role": m.get("role"),
                "content": m.get("encrypted_content"),
                "created_at": m.get("created_at")
            })
        return messages

    except Exception as e:
        logger.error(f"Error fetching history for session {session_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to load chat history")


# ----------------------
# CSV Import confirm
# ----------------------
@router.post("/import-confirm")
async def confirm_import(
    payload: Dict[str, Any],
    current_user: Dict[str, Any] = Depends(get_current_user),
    supabase: Client = Depends(get_authenticated_client)
):
    try:
        user_id = current_user["sub"]
        session_id = payload.get("session_id")

        # -----------------------------------------------------------
        # 1. QUOTA CHECK: Import Limits & Trade Storage
        # -----------------------------------------------------------
        profile = await get_full_user_profile(user_id)
        
        # Check monthly import count
        QuotaManager.check_usage_limit(
            profile, 
            limit_key="monthly_csv_imports", 
            current_usage_key="monthly_import_count"
        )

        # Check total trade storage limit
        await QuotaManager.check_trade_storage_limit(user_id, profile)
        # -----------------------------------------------------------

        # Download file bytes from storage
        res = supabase.storage.from_("temp-imports").download(payload["file_path"])
        raw_trades = csv_parser.process_and_normalize(res, payload["mapping"])

        valid_trades: List[Dict[str, Any]] = []
        failed_rows: List[Dict[str, Any]] = []

        for i, raw_trade in enumerate(raw_trades):
            try:
                if "entry_time" not in raw_trade or not raw_trade["entry_time"]:
                    raw_trade["entry_time"] = datetime.now()
                if "status" not in raw_trade:
                    raw_trade["status"] = "CLOSED" if raw_trade.get("exit_price") else "OPEN"

                trade_model = TradeCreate(**raw_trade)
                db_row = serialize_for_db(trade_model.dict(exclude={"notes"}))
                if trade_model.notes:
                    # notes remain encrypted in trades table (handled elsewhere)
                    db_row["encrypted_notes"] = trade_model.notes
                db_row["user_id"] = user_id

                if "pnl" not in db_row or db_row["pnl"] is None:
                    if trade_model.exit_price and trade_model.exit_price > 0:
                        mult = 1.0 if trade_model.direction == "LONG" else -1.0
                        pnl = (trade_model.exit_price - trade_model.entry_price) * trade_model.quantity * mult - trade_model.fees
                        db_row["pnl"] = str(pnl)

                valid_trades.append(db_row)

            except Exception as e:
                logger.warning("Row %s validation failed: %s", i, e)
                failed_rows.append({"row": i + 1, "error": str(e), "data": raw_trade})

        if valid_trades:
            BATCH_SIZE = 100
            for k in range(0, len(valid_trades), BATCH_SIZE):
                batch = valid_trades[k:k + BATCH_SIZE]
                supabase.table("trades").insert(batch).execute()

        # remove temp file
        supabase.storage.from_("temp-imports").remove([payload["file_path"]])

        # -----------------------------------------------------------
        # 2. QUOTA INCREMENT: Count the import success
        # -----------------------------------------------------------
        await QuotaManager.increment_usage(user_id, "csv_import")
        # -----------------------------------------------------------

        msg = f"Successfully imported {len(valid_trades)} trades."
        if failed_rows:
            msg += f" {len(failed_rows)} rows failed validation."

        # Log summary to chat (PLAINTEXT; aligned with chat decision)
        try:
            if not session_id:
                topic = f"Import: {len(valid_trades)} trades"
                sess_res = supabase.table("chat_sessions").insert({"user_id": user_id, "topic": topic}).execute()
                if sess_res.data:
                    session_id = sess_res.data[0]["id"]
                    logger.info("Created new session %s for import log", session_id)

            if session_id:
                log_content = f"✅ **Import Complete**\n{msg}"
                supabase.table("chat_messages").insert({
                    "session_id": session_id,
                    "user_id": user_id,
                    "role": "assistant",
                    "encrypted_content": log_content  # plaintext intentionally
                }).execute()
        except Exception as log_e:
            logger.warning("Failed to log import success to chat: %s", log_e)

        logger.info("User %s imported %s trades.", user_id, len(valid_trades))
        return {
            "status": "success",
            "count": len(valid_trades),
            "failures": len(failed_rows),
            "failed_samples": failed_rows[:5],
            "message": msg,
            "session_id": session_id
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Import execution failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Import failed: {str(e)}")


# ----------------------
# Main chat handler
# ----------------------
@router.post("", response_model=ChatResponse)
async def chat_with_ai(
    request: ChatRequest,
    background_tasks: BackgroundTasks,
    # 👇 This dependency now automatically:
    # 1. Fetches the live user profile
    # 2. Checks the daily message limit (402 error if exceeded)
    # 3. Sets the '_needs_daily_reset' flag if it's a new day
    user_profile: Dict[str, Any] = Depends(check_ai_quota),
    supabase: Client = Depends(get_authenticated_client)
):
    # Extract user_id directly from the profile returned by permissions.py
    user_id = str(user_profile["id"])
    session_id = request.session_id
    raw_message = (request.message or "").strip()

    # Capture the reset flag (set inside check_ai_quota -> QuotaManager)
    needs_reset = user_profile.get("_needs_daily_reset", False)

    # Support explicit flag and legacy prefix
    web_search_flag = bool(getattr(request, "web_search", False))
    if "[WEB SEARCH]" in raw_message:
        web_search_flag = True
        raw_message = raw_message.replace("[WEB SEARCH]", "").strip()
        logger.info("Web search (legacy prefix) activated for user %s", user_id)

    # -----------------------------------------------------------
    # FEATURE CHECK: Web Search
    # -----------------------------------------------------------
    if web_search_flag:
        # We already have the profile, just check the feature flag
        QuotaManager.check_feature_access(user_profile, "allow_web_search")

    if not raw_message:
        raise HTTPException(status_code=400, detail="Empty message")

    is_new_session = False

    try:
        # --- Create session early so messages are associated immediately ---
        if not session_id:
            is_new_session = True
            initial_topic = (raw_message[:45] + "...") if len(raw_message) > 45 else raw_message
            sess_res = supabase.table("chat_sessions").insert({"user_id": user_id, "topic": initial_topic}).execute()
            if not sess_res.data:
                raise HTTPException(status_code=500, detail="Failed to create session")
            session_id = sess_res.data[0]["id"]
            background_tasks.add_task(generate_session_title, session_id, raw_message, supabase)

        # --- Session-memory shortcut (no LLM, fast) ---
        if is_session_memory_query(raw_message):
            try:
                rows = supabase.table("chat_messages") \
                    .select("encrypted_content, role, created_at") \
                    .eq("session_id", session_id) \
                    .eq("role", "user") \
                    .order("created_at", desc=True) \
                    .limit(2) \
                    .execute()
                data = rows.data or []
                if len(data) >= 2:
                    prev = data[1]["encrypted_content"]  # plaintext
                    return {"response": prev, "session_id": session_id, "usage": {"total_tokens": 0}}
                else:
                    return {"response": "I don't see a previous user message in this session.", "session_id": session_id, "usage": {"total_tokens": 0}}
            except Exception as e:
                logger.warning("Session memory shortcut failed: %s", e)
                # fall-through to normal pipeline

        # --- Build conversation context (exclude current message) ---
        recent_context = fetch_recent_messages(supabase, session_id, limit=RECENT_CONTEXT_LIMIT)

        # --- Persist user message BEFORE heavy processing (best-effort) ---
        try:
            supabase.table("chat_messages").insert({
                "session_id": session_id, "user_id": user_id, "role": "user", "encrypted_content": raw_message
            }).execute()
        except Exception as e:
            logger.warning("Failed to persist user message: %s", e)

        # --- Conservative trade drafting (guarded LLM) ---
        trade_proposal = None
        if not web_search_flag and is_trade_proposal_trigger(raw_message):
            try:
                trade_proposal = await parse_trade_intent(raw_message)
            except Exception as e:
                logger.warning("parse_trade_intent failed: %s", e)

        if trade_proposal:
            ai_response_text = "I've drafted this trade based on your message. Please review and confirm."
            try:
                supabase.table("chat_messages").insert({
                    "session_id": session_id, "user_id": user_id, "role": "assistant", "encrypted_content": ai_response_text
                }).execute()
            except Exception:
                pass
            return {
                "response": ai_response_text,
                "session_id": session_id,
                "usage": {"total_tokens": 0},
                "tool_call": {"type": "trade-confirmation", "data": trade_proposal}
            }

        # --- If web_search_flag: route to Perplexity via llm_client ---
        web_search_snapshot: Optional[Dict[str, Any]] = None
        response_text: str
        if web_search_flag:
            try:
                response_text = await run_web_search_with_perplexity(raw_message, recent_context)
            except Exception as e:
                logger.warning("Web search failed; falling back to pipeline: %s", e)
                try:
                    response_text = await process_chat_message(user_id, raw_message)
                except TypeError:
                    response_text = await process_chat_message(user_id, raw_message)
        else:
            # Normal deterministic pipeline path. Pass conversation context and optional snapshot.
            try:
                response_text = await process_chat_message(
                    user_id=user_id,
                    message=raw_message,
                    conversation_context=recent_context,
                    web_search_snapshot=web_search_snapshot
                )
            except TypeError:
                # backward compatibility: older pipeline expected (user_id, message)
                response_text = await process_chat_message(user_id, raw_message)
            except Exception as e:
                logger.exception("Pipeline processing failed: %s", e)
                fallback_msg = ("I'm having trouble processing that request right now. "
                                "Your message has been saved — please try again in a moment.")
                try:
                    supabase.table("chat_messages").insert({
                        "session_id": session_id, "user_id": user_id, "role": "assistant", "encrypted_content": fallback_msg
                    }).execute()
                except Exception:
                    pass
                return {"response": fallback_msg, "session_id": session_id, "usage": {"total_tokens": 0}}

        # --- Persist assistant reply (best-effort) ---
        try:
            supabase.table("chat_messages").insert({
                "session_id": session_id, "user_id": user_id, "role": "assistant", "encrypted_content": response_text
            }).execute()
        except Exception as e:
            logger.warning("Failed to persist assistant reply: %s", e)

        # -----------------------------------------------------------
        # 3. QUOTA INCREMENT (BACKGROUND TASK)
        # -----------------------------------------------------------
        # Estimate usage (user input + assistant output)
        total_tokens = estimate_tokens(raw_message) + estimate_tokens(response_text)
        
        # We fire this off in the background so the user gets the response faster.
        # This handles the DB update asynchronously.
        background_tasks.add_task(
            QuotaManager.increment_usage,
            user_id, 
            "chat_message", 
            extra_data={
                "needs_reset": needs_reset,
                "tokens": total_tokens
            }
        )
        # -----------------------------------------------------------

        # --- Possibly update rolling summary asynchronously (DISABLED by default) ---
        try:
            if SUMMARY_ENABLED:
                count_res = supabase.table("chat_messages").select("id", count="exact").eq("session_id", session_id).execute()
                message_count = getattr(count_res, "count", None) or (len(recent_context) + 1)
                if isinstance(message_count, int) and message_count > 0 and (message_count % SUMMARY_UPDATE_THRESHOLD == 0):
                    background_tasks.add_task(update_conversation_summary_background, supabase, session_id, recent_context)
        except Exception:
            # don't fail the request on summary errors
            pass

        # --- Return the response (structured if possible) ---
        try:
            parsed = json.loads(response_text)
            return {"response": json.dumps(parsed, ensure_ascii=False), "session_id": session_id, "usage": {"total_tokens": total_tokens}}
        except Exception:
            return {"response": response_text, "session_id": session_id, "usage": {"total_tokens": total_tokens}}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Chat processing failed: %s", e)
        if is_new_session and session_id:
            try:
                supabase.table("chat_sessions").delete().eq("id", session_id).execute()
            except Exception:
                logger.warning("Failed to clean up session after failure")
        raise HTTPException(status_code=502, detail="AI Service Error: temporarily unavailable")