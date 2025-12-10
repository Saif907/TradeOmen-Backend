# backend/app/apis/v1/chat/services.py
import json
import logging
from typing import List, Dict, Any, Optional
from supabase import Client
from app.lib.llm_client import llm_client

# --- Configuration & Logging ---
logger = logging.getLogger(__name__)

async def generate_session_title(session_id: str, first_message: str, supabase: Client):
    """
    Background task to generate a short, relevant title.
    """
    try:
        messages = [
            {"role": "system", "content": "Summarize user's message in 3-5 words for a chat title. No quotes. No preamble."},
            {"role": "user", "content": first_message}
        ]
        # Use a cheaper/faster model for titles if available, otherwise default
        result = await llm_client.generate_response(
            messages, 
            model="gemini-2.5-flash", 
            provider="gemini", 
            max_tokens=20
        )
        title = result["content"].strip().replace('"', '')
        
        if title:
            # Update title
            supabase.table("chat_sessions").update({"topic": title}).eq("id", session_id).execute()
            logger.debug(f"Generated title for session {session_id}: {title}")
            
    except Exception as e:
        logger.error(f"Title generation failed for session {session_id}: {e}")

async def parse_trade_intent(message: str) -> Optional[Dict[str, Any]]:
    """
    Detects if the user wants to log a trade and extracts details.
    Uses strict JSON mode enforcement.
    """
    # 1. Heuristic Check (Fast fail to save API calls)
    keywords = ["log", "add", "record", "bought", "sold", "shorted", "long", "short", "buy", "sell"]
    if not any(k in message.lower() for k in keywords):
        return None
        
    logger.info("Analyzing message for trade intent...")

    system_prompt = """
    You are a precise trading assistant. Extract trade details from the user's message into a strict JSON format.
    
    Required Fields:
    - symbol: string (uppercase, e.g., "AAPL", "BTC-USD")
    - direction: string ("Long" or "Short")
    - quantity: float (must be > 0)
    - entry_price: float (must be > 0)
    
    Optional Fields (use null if missing):
    - stop_loss: float
    - target: float
    - notes: string (any context or reasoning provided)
    - instrument_type: string ("STOCK", "CRYPTO", "FOREX", "FUTURES") - infer from symbol if possible, default "STOCK"
    
    Rules:
    1. If critical info (symbol, price, qty) is missing, return NULL (do not hallucinate).
    2. Convert "bought"/"buy" to "Long" and "sold"/"sell" to "Short".
    3. Return ONLY valid JSON.
    """
    
    try:
        response = await llm_client.generate_response(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": message}
            ],
            model="gemini-2.5-pro", # Use high-reasoning model for extraction
            provider="gemini",
            # Force JSON mode if provider supports it (OpenAI does)
            response_format={"type": "json_object"} 
        )
        
        content = response["content"].strip()
        
        # Clean potential markdown wrappers
        if content.startswith("```json"):
            content = content.replace("```json", "").replace("```", "")
            
        data = json.loads(content)
        
        # Validation: Ensure critical fields exist
        required_fields = ["symbol", "direction", "entry_price", "quantity"]
        if not all(data.get(k) for k in required_fields):
            logger.warning("Trade intent detected but missing critical fields.")
            return None

        # Normalization
        data["symbol"] = data["symbol"].upper()
        data["direction"] = data["direction"].title()
        
        logger.info(f"Trade details extracted: {data['symbol']} {data['direction']}")
        return data
        
    except json.JSONDecodeError:
        logger.error("LLM returned invalid JSON for trade parsing")
        return None
    except Exception as e:
        logger.error(f"Trade parsing failed: {e}")
        return None

def build_memory_context(session_id: str, supabase: Client, limit: int = 10) -> List[Dict[str, str]]:
    """
    Fetches recent chat history for context.
    """
    try:
        res = supabase.table("chat_messages")\
            .select("role, encrypted_content")\
            .eq("session_id", session_id)\
            .order("id", desc=True)\
            .limit(limit)\
            .execute()
        
        # Return Oldest -> Newest
        history = [{"role": m["role"], "content": m["encrypted_content"]} for m in reversed(res.data)]
        return history
    except Exception as e:
        logger.error(f"Failed to build memory context: {e}")
        return []

def build_trading_context(supabase: Client) -> str:
    """
    Fetches recent trades and strategies to ground the AI's responses.
    Optimized to select only necessary columns.
    """
    try:
        # 1. Fetch Recent Trades (Optimized Selection)
        trades = supabase.table("trades")\
            .select("symbol, direction, entry_price, pnl, status, entry_time")\
            .order("entry_time", desc=True)\
            .limit(5)\
            .execute()
            
        # 2. Fetch Active Strategies
        strategies = supabase.table("strategies")\
            .select("name, description, style")\
            .limit(5)\
            .execute()
        
        context_parts = []
        
        if trades.data:
            context_parts.append("Recent Trades:")
            for t in trades.data:
                # Visual cue for the LLM
                status_icon = "🟢" if t.get('pnl') and t['pnl'] > 0 else "🔴" if t.get('pnl') and t['pnl'] < 0 else "⚪"
                pnl_str = f"${t['pnl']:.2f}" if t.get('pnl') is not None else "Open"
                
                context_parts.append(f"- {t['symbol']} {t['direction']} ({t['status']}): PnL {pnl_str} {status_icon}")
        else:
            context_parts.append("No recent trades recorded.")
            
        if strategies.data:
            context_parts.append("\nActive Strategies:")
            for s in strategies.data:
                context_parts.append(f"- {s['name']} ({s.get('style', 'General')}): {s.get('description', 'No description')}")
                
        return "\n".join(context_parts)
        
    except Exception as e:
        logger.error(f"Failed to build trading context: {e}")
        return "Context unavailable due to system error."