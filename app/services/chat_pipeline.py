# backend/app/services/chat_pipeline.py
import logging
import json
import re
from typing import Dict, Any, List, Optional
from uuid import UUID

from app.core.database import db
from app.lib.llm_client import llm_client
from app.lib.encryption import crypto

logger = logging.getLogger(__name__)

# -------------------------------------------------------------------------
# 1. INTENT DEFINITIONS
# -------------------------------------------------------------------------

class Intent:
    SUMMARY = "SUMMARY"           # Aggregated stats (Win rate, PnL, etc.)
    SAMPLE = "SAMPLE"             # List of recent/best/worst trades
    SINGLE_TRADE = "SINGLE_TRADE" # Deep dive into one specific trade
    SIMILAR = "SIMILAR"           # Vector search / semantic
    MARKET = "MARKET"             # Market alignment (Perplexity)
    GENERAL = "GENERAL"           # Chitchat / General trading questions

# -------------------------------------------------------------------------
# 2. INTENT CLASSIFICATION (Structured output)
# -------------------------------------------------------------------------

async def classify_intent(message: str) -> Dict[str, Any]:
    """
    Deterministic rule-based classification with LLM JSON fallback.
    Returns:
      {
        "intent": one of Intent.*,
        "modifiers": {
          "count": Optional[int],
          "order": Optional["TOP"|"RECENT"],
          "time_range": Optional[str],
          "symbol": Optional[str],
          "strategy": Optional[str],
          "raw": original message
        },
        "confidence": float (0.0-1.0)
      }
    Rules-first approach: deterministic extractors + conservative LLM fallback when ambiguous.
    """
    msg = (message or "").strip()
    msg_l = msg.lower()

    modifiers: Dict[str, Any] = {"raw": msg}

    # ---------- Helper extractors ----------
    def extract_count_order(text: str) -> Optional[Dict[str, Any]]:
        # Examples: "top 25", "last 10", "recent 5", "show me top 25 trades"
        m = re.search(r'\b(top|last|recent)\b\s*(?:the\s*)?(\d{1,3})', text)
        if m:
            kind = m.group(1)
            cnt = int(m.group(2))
            order = "TOP" if kind == "top" else "RECENT"
            return {"count": cnt, "order": order}
        # also handle "25 best", "25 worst"
        m2 = re.search(r'\b(\d{1,3})\s+(best|worst|recent)\b', text)
        if m2:
            cnt = int(m2.group(1))
            kind2 = m2.group(2)
            order = "TOP" if kind2 == "best" else "RECENT"
            return {"count": cnt, "order": order}
        return None

    def extract_time_range(text: str) -> Optional[str]:
        # simple common patterns
        patterns = [
            (r'\blast\s+(\d+)\s+days?\b', lambda n: f"LAST_{n}_DAYS"),
            (r'\blast\s+week\b', lambda _: "LAST_7_DAYS"),
            (r'\blast\s+month\b', lambda _: "LAST_30_DAYS"),
            (r'\bthis\s+week\b', lambda _: "THIS_WEEK"),
            (r'\bthis\s+month\b', lambda _: "THIS_MONTH"),
            (r'\b(last quarter|previous quarter)\b', lambda _: "LAST_QUARTER"),
        ]
        for pat, fn in patterns:
            m = re.search(pat, text)
            if m:
                return fn(m.group(1)) if m.groups() else fn(None)
        return None

    def extract_symbol(text: str) -> Optional[str]:
        # crude symbol extraction (all caps or prefixed with $)
        m = re.search(r'\$\b([A-Z0-9\.]{1,8})\b', message)
        if m:
            return m.group(1).upper()
        # fallback: all-caps token 2-6 length (NSE tickers often mixed but this is heuristic)
        m2 = re.search(r'\b([A-Z]{2,6})\b', message)
        if m2 and len(m2.group(1)) <= 6:
            token = m2.group(1)
            # avoid matching common words: filter basic english words (very small list)
            blacklist = {"THE","AND","FOR","WITH","THIS","THAT","YOUR","HOW","WHY"}
            if token not in blacklist:
                return token
        return None

    def extract_strategy(text: str) -> Optional[str]:
        # look for "breakout", "mean reversion", "momentum", etc.
        known = ["breakout", "reversion", "mean reversion", "momentum", "trend", "scalp", "swing", "pre-market"]
        for k in known:
            if k in text:
                return k
        return None

    # ---------- Populate modifiers ----------
    count_info = extract_count_order(msg_l)
    if count_info:
        modifiers.update(count_info)

    tr = extract_time_range(msg_l)
    if tr:
        modifiers["time_range"] = tr

    sym = extract_symbol(msg)
    if sym:
        modifiers["symbol"] = sym

    strat = extract_strategy(msg_l)
    if strat:
        modifiers["strategy"] = strat

    # ---------- Deterministic intent decisions ----------
    # SINGLE_TRADE signals (explicit)
    single_signals = [
        r'\b(this trade|that trade|this position|that position|this entry|that entry)\b',
        r'\b(what went wrong|why did i lose|explain this|break down this|break down my)\b',
        r'\b(analyze (this|that)? trade|deep dive|deep-dive|trade details|trade details for)\b'
    ]
    for pat in single_signals:
        if re.search(pat, msg_l):
            return {"intent": Intent.SINGLE_TRADE, "modifiers": modifiers, "confidence": 0.95}

    # SAMPLE / LIST signals
    sample_signals = [
        r'\b(show me|list|display|give me)\b.*\b(trades|entries|positions)\b',
        r'\b(top \d+|last \d+|recent \d+|best trades|worst trades|recent trades|top trades)\b'
    ]
    for pat in sample_signals:
        if re.search(pat, msg_l):
            # if count > 100 reject and fallback to UI-only (still SAMPLE intent)
            if modifiers.get("count") and modifiers["count"] > 500:
                modifiers["count"] = 500
            return {"intent": Intent.SAMPLE, "modifiers": modifiers, "confidence": 0.9}

    # SUMMARY signals
    if any(k in msg_l for k in ["summary", "how am i doing", "overall performance", "stats", "win rate", "profit factor", "total pnl", "expectancy", "report"]):
        return {"intent": Intent.SUMMARY, "modifiers": modifiers, "confidence": 0.95}

    # MARKET signals (make sure it's explicitly market focused)
    if any(k in msg_l for k in ["market conditions", "market today", "volatility", "sentiment", "news today", "macro", "futures", "pre-market", "market snapshot"]):
        return {"intent": Intent.MARKET, "modifiers": modifiers, "confidence": 0.92}

    # RISK / RULE CHECKS (treat as SAMPLE+SUMMARY hybrid under the hood)
    if re.search(r'\b(risk|stop loss|follow.*rule|rule violation|did i follow|max position)\b', msg_l):
        return {"intent": Intent.SAMPLE, "modifiers": {**modifiers, "focus": "risk"}, "confidence": 0.88}

    # If nothing matched confidently, call the LLM fallback to produce JSON
    # LLM fallback should return a JSON object with intent and modifiers
    try:
        fallback_system = (
            "You are an intent extraction assistant for a trading journal application. "
            "Given a user's natural language query, return a JSON object with keys: "
            "'intent' (one of SUMMARY, SAMPLE, SINGLE_TRADE, MARKET, GENERAL), "
            "'modifiers' (object with optional keys count, order, time_range, symbol, strategy), "
            "and 'confidence' (0.0-1.0). Return ONLY valid JSON."
            "\n\nExamples:\n"
            '{"intent":"SUMMARY","modifiers":{},"confidence":0.95}\n'
            '{"intent":"SAMPLE","modifiers":{"count":25,"order":"TOP"},"confidence":0.9}\n'
            '{"intent":"SINGLE_TRADE","modifiers":{"symbol":"AAPL"},"confidence":0.98}\n'
        )

        response = await llm_client.generate_response(
            messages=[
                {"role": "system", "content": fallback_system},
                {"role": "user", "content": msg}
            ],
            model="gemini-2.5-flash",
            provider="gemini",
            # note: ensure llm_client.generate_response returns a dict with 'content'
        )

        content = response.get("content", "").strip()
        # attempt to parse JSON out of the content
        parsed = None
        try:
            parsed = json.loads(content)
            # basic validation
            intent_val = parsed.get("intent", "").upper()
            if intent_val not in [Intent.SUMMARY, Intent.SAMPLE, Intent.SINGLE_TRADE, Intent.MARKET, Intent.GENERAL]:
                raise ValueError("Invalid intent from LLM fallback")
            # normalize modifiers
            parsed_mods = parsed.get("modifiers", {}) or {}
            parsed_conf = float(parsed.get("confidence", 0.7))
            return {"intent": intent_val, "modifiers": {**modifiers, **parsed_mods}, "confidence": parsed_conf}
        except Exception:
            logger.warning("LLM fallback returned invalid JSON; falling back to GENERAL. Content: %s", content)
    except Exception as e:
        logger.exception("LLM fallback failed for intent classification: %s", str(e))

    # final safe fallback
    return {"intent": Intent.GENERAL, "modifiers": modifiers, "confidence": 0.5}

# -------------------------------------------------------------------------
# 3. DATA RETRIEVAL (The "Backend Math")
#    (unchanged except minor routing updates to expect structured classification)
# -------------------------------------------------------------------------

class TradeDataManager:
    def __init__(self, user_id: str):
        self.user_id = user_id

    async def get_summary_stats(self) -> Dict[str, Any]:
        """
        Calculates stats in DB. No LLM math.
        """
        query = """
        SELECT 
            COUNT(*) as total_trades,
            COALESCE(SUM(pnl), 0) as total_pnl,
            COUNT(CASE WHEN pnl > 0 THEN 1 END) as wins,
            COUNT(CASE WHEN pnl < 0 THEN 1 END) as losses,
            COALESCE(AVG(pnl), 0) as avg_pnl,
            MAX(pnl) as best_trade,
            MIN(pnl) as worst_trade
        FROM trades 
        WHERE user_id = $1 AND status = 'CLOSED'
        """
        row = await db.fetch_one(query, self.user_id)
        if not row:
            return {"error": "No closed trades found"}

        total = int(row["total_trades"] or 0)
        wins = int(row["wins"] or 0)
        losses = int(row["losses"] or 0)
        win_rate = (wins / total * 100) if total > 0 else 0

        return {
            "type": "SUMMARY",
            "stats": {
                "total_trades": total,
                "total_pnl": float(row["total_pnl"]),
                "win_rate": round(win_rate, 2),
                "avg_pnl": round(float(row["avg_pnl"]), 4),
                "best_trade": float(row["best_trade"] or 0),
                "worst_trade": float(row["worst_trade"] or 0),
                "profit_factor": None  # compute separately if breakdown by wins/losses and fees available
            }
        }

    async def get_trade_samples(self, modifiers: Dict[str, Any]) -> Dict[str, Any]:
        """
        Curated sampling: respects modifiers (count/order/time_range).
        Caps sample count sent to AI at 10; UI can still request larger lists.
        """
        # cap for AI consumption
        requested_count = modifiers.get("count") or 10
        order = modifiers.get("order") or "RECENT"
        # cap values
        sample_limit_for_ai = min(requested_count, 10)

        # Build queries: recent, best, worst (we will always return the union)
        recent_q = "(SELECT id, symbol, direction, pnl, entry_time, 'RECENT' as label FROM trades WHERE user_id = $1 ORDER BY entry_time DESC LIMIT $2)"
        best_q = "(SELECT id, symbol, direction, pnl, entry_time, 'BEST' as label FROM trades WHERE user_id = $1 AND pnl > 0 ORDER BY pnl DESC LIMIT 3)"
        worst_q = "(SELECT id, symbol, direction, pnl, entry_time, 'WORST' as label FROM trades WHERE user_id = $1 AND pnl < 0 ORDER BY pnl ASC LIMIT 3)"

        union_q = f"{recent_q} UNION ALL {best_q} UNION ALL {worst_q}"
        rows = await db.fetch_all(union_q, self.user_id, sample_limit_for_ai)
        sanitized_trades = []
        for r in rows:
            sanitized_trades.append({
                "id": str(r["id"]),
                "symbol": r["symbol"],
                "pnl": float(r["pnl"] or 0),
                "date": r["entry_time"].strftime("%Y-%m-%d"),
                "label": r["label"]
            })

        return {
            "type": "SAMPLE",
            "count": len(sanitized_trades),
            "data": sanitized_trades
        }

    async def get_single_trade_deep_dive(self, context_text: str, modifiers: Dict[str, Any]) -> Dict[str, Any]:
        """
        Fetches full row + decrypts notes.
        The function uses modifiers first (symbol) then heuristics on context_text.
        """
        symbol = modifiers.get("symbol")
        if not symbol:
            # attempt to extract from message heuristics
            m = re.search(r'\b([A-Z]{2,6})\b', context_text)
            if m:
                symbol = m.group(1)

        if symbol:
            query = "SELECT * FROM trades WHERE user_id = $1 AND symbol = $2 ORDER BY entry_time DESC LIMIT 1"
            row = await db.fetch_one(query, self.user_id, symbol)
        else:
            query = "SELECT * FROM trades WHERE user_id = $1 ORDER BY entry_time DESC LIMIT 1"
            row = await db.fetch_one(query, self.user_id)

        if not row:
            return {"error": "Trade not found"}

        # Decrypt notes if present
        notes = None
        if row.get("encrypted_notes"):
            try:
                notes = crypto.decrypt(row["encrypted_notes"])
            except Exception:
                notes = "[Decryption Failed]"

        # include strategy lookup if available
        strategy = None
        try:
            if row.get("strategy_id"):
                q = "SELECT id, name, rules FROM strategies WHERE id = $1"
                s = await db.fetch_one(q, row["strategy_id"])
                if s:
                    strategy = {"id": str(s["id"]), "name": s["name"], "rules": s["rules"]}
        except Exception:
            pass

        return {
            "type": "SINGLE_TRADE",
            "data": {
                "id": str(row["id"]),
                "symbol": row["symbol"],
                "entry_price": float(row["entry_price"] or 0),
                "exit_price": float(row["exit_price"] or 0) if row.get("exit_price") is not None else None,
                "pnl": float(row["pnl"] or 0),
                "entry_time": row["entry_time"].isoformat() if row.get("entry_time") else None,
                "exit_time": row["exit_time"].isoformat() if row.get("exit_time") else None,
                "notes": notes,
                "tags": row.get("tags"),
                "emotion": row.get("emotion"),
                "metadata": row.get("metadata") or {},
                "strategy": strategy
            }
        }

# -------------------------------------------------------------------------
# 4. PAYLOAD CONSTRUCTOR (Prompt Guard)
# -------------------------------------------------------------------------

def construct_system_prompt(intent: str, data: Dict[str, Any]) -> str:
    """
    Builds an AI-safe prompt that forces the LLM to use the provided data
    as Ground Truth.
    """
    base_guard = """
    CRITICAL INSTRUCTIONS:
    1. You are a Trading Analyst.
    2. Use the PROVIDED DATA below as Ground Truth.
    3. DO NOT recalculate metrics (like Win Rate); use the pre-calculated values.
    4. DO NOT invent trades or numbers.
    5. If the data indicates an error or is missing, say so explicitly.
    """

    if intent == Intent.SUMMARY:
        return f"""
        {base_guard}
        CONTEXT: User requested a performance SUMMARY.
        DATA: {json.dumps(data, indent=2)}
        TASK: Summarize performance in 3 short bullets, call out strengths, weaknesses, and one action item.
        """

    elif intent == Intent.SAMPLE:
        return f"""
        {base_guard}
        CONTEXT: User requested a SAMPLE / list of trades.
        DATA: {json.dumps(data, indent=2)}
        TASK: Present these trades clearly. Highlight patterns between 'BEST' and 'WORST' labels. Do not assume trades outside this list.
        """

    elif intent == Intent.SINGLE_TRADE:
        return f"""
        {base_guard}
        CONTEXT: Deep dive analysis of a specific trade.
        DATA: {json.dumps(data, indent=2)}
        TASK: Analyze this trade. Use 'notes' to surface behavior and decision points. Give actionable feedback and one concrete improvement.
        """

    elif intent == Intent.MARKET:
        return f"""
        {base_guard}
        CONTEXT: Market alignment analysis.
        DATA: {json.dumps(data, indent=2)}
        TASK: Explain whether the market snapshot aligns with strategy rules supplied. Do not provide trading advice.
        """

    # General fallback
    return f"{base_guard}\nDATA: {json.dumps(data, indent=2)}\nTASK: Provide a concise reply."

# -------------------------------------------------------------------------
# 5. MAIN PIPELINE ENTRY POINT (updated to use structured classification)
# -------------------------------------------------------------------------

async def process_chat_message(
    user_id: str,
    message: str,
    conversation_context: Optional[List[Dict[str, str]]] = None,
    web_search_snapshot: Optional[Dict[str, Any]] = None,
) -> str:
    # NOTE:
    # conversation_context is intentionally unused for now.
    # It is accepted to maintain compatibility with the router
    # and will be used later for deeper multi-turn reasoning.

    # 1. Identify Intent
    classification = await classify_intent(message)
    intent = classification.get("intent", Intent.GENERAL)
    modifiers = classification.get("modifiers", {}) or {}
    confidence = classification.get("confidence", 0.5)

    logger.info(
        "User %s | Intent: %s | Modifiers: %s | Confidence: %.2f",
        user_id, intent, modifiers, float(confidence)
    )

    data_manager = TradeDataManager(user_id)

    # 2. Fetch deterministic data
    if intent == Intent.SUMMARY:
        context_data = await data_manager.get_summary_stats()
    elif intent == Intent.SAMPLE:
        context_data = await data_manager.get_trade_samples(modifiers)
    elif intent == Intent.SINGLE_TRADE:
        context_data = await data_manager.get_single_trade_deep_dive(message, modifiers)
    elif intent == Intent.MARKET:
        context_data = {
            "type": "MARKET",
            "snapshot": web_search_snapshot
            or {"info": "Market alignment snapshot unavailable."}
        }
    else:
        context_data = {
            "type": "GENERAL",
            "info": "General trading conversation or help."
        }

    # 3. Prompt construction
    system_prompt = construct_system_prompt(intent, context_data)

    # 4. LLM call (single, controlled)
    response = await llm_client.generate_response(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": message}
        ],
        model="gemini-2.5-flash",
        provider="gemini"
    )

    return response.get("content", "").strip()
