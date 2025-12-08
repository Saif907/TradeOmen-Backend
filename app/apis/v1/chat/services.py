# backend/app/apis/v1/chat/services.py
import json
from typing import List, Dict, Any, Optional
from supabase import Client
from app.lib.llm_client import llm_client

async def generate_session_title(session_id: str, first_message: str, supabase: Client):
    """Background task to generate a short, relevant title."""
    try:
        messages = [
            {"role": "system", "content": "Summarize user's message in 3-5 words for a chat title. No quotes."},
            {"role": "user", "content": first_message}
        ]
        result = await llm_client.generate_response(
            messages, 
            model="gemini-2.5-flash", 
            provider="gemini", 
            max_tokens=15
        )
        title = result["content"].strip().replace('"', '')
        if title:
            supabase.table("chat_sessions").update({"topic": title}).eq("id", session_id).execute()
    except Exception as e:
        print(f"Title generation failed: {e}")

async def parse_trade_intent(message: str) -> Optional[Dict[str, Any]]:
    """
    Detects if the user wants to log a trade and extracts details.
    """
    # 1. Heuristic Check
    if not any(k in message.lower() for k in ["log", "add", "record", "bought", "sold", "shorted", "long", "short"]):
        return None
        
    # ✅ FIX: Explicitly ask for "Long" / "Short" (Title Case) to match trades.py
    system_prompt = """
    Extract trade details from the user's message into JSON.
    
    Fields: 
    - symbol (uppercase)
    - direction (Must be "Long" or "Short" - Title Case)
    - entry_price (float)
    - quantity (float)
    - stop_loss (float/null)
    - target (float/null)
    - notes (string)
    
    If a field is missing, use null.
    Example: "Bought 10 AAPL at 150" -> {"symbol": "AAPL", "direction": "Long", "entry_price": 150, "quantity": 10}
    RETURN ONLY JSON.
    """
    
    try:
        response = await llm_client.generate_response(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": message}
            ],
            model="gemini-2.5-flash", 
            provider="gemini"
        )
        
        content = response["content"].replace("```json", "").replace("```", "").strip()
        data = json.loads(content)
        
        # Fallback normalization just in case AI ignores instructions
        if data.get("direction"):
            data["direction"] = data["direction"].title() # Ensure "Long" not "LONG"
            
        return data
    except Exception as e:
        print(f"Trade parsing failed: {e}")
        return {
            "symbol": "UNKNOWN",
            "direction": "Long", 
            "entry_price": 0,
            "quantity": 1,
            "notes": message
        }

def build_memory_context(session_id: str, supabase: Client, limit: int = 10) -> List[Dict[str, str]]:
    try:
        res = supabase.table("chat_messages")\
            .select("role, encrypted_content")\
            .eq("session_id", session_id)\
            .order("id", desc=True)\
            .limit(limit)\
            .execute()
        
        # Reverse to chronological order (Oldest -> Newest)
        history = [{"role": m["role"], "content": m["encrypted_content"]} for m in reversed(res.data)]
        return history
    except:
        return []

def build_trading_context(supabase: Client) -> str:
    try:
        trades = supabase.table("trades").select("*").order("entry_time", desc=True).limit(5).execute()
        strategies = supabase.table("strategies").select("name, description").execute()
        
        context = "Recent Trades:\n"
        if trades.data:
            for t in trades.data:
                symbol = t.get('symbol', 'Unknown')
                direction = t.get('direction', '-')
                pnl = t.get('pnl', 'N/A')
                context += f"- {symbol} {direction}: PnL {pnl}\n"
        else:
            context += "- No recent trades.\n"
            
        context += "\nStrategies:\n"
        if strategies.data:
            for s in strategies.data:
                context += f"- {s.get('name')}: {s.get('description')}\n"
        return context
    except Exception as e:
        return ""