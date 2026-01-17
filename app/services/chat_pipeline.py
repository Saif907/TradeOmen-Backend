# backend/app/services/chat_pipeline.py

import logging
import json
import re
import uuid
from typing import Dict, Any, List, Optional

from app.core.config import settings
from app.core.database import db
from app.lib.llm_client import llm_client
from app.services.chat_tools import ChatTools
from app.services.quota_manager import QuotaManager
from app.services.metrics_engine import MetricsEngine
from app.auth.dependency import update_user_cache  # ✅ Import for cache sync

logger = logging.getLogger("tradeomen.chat_pipeline")

# -------------------------
# CONSTANTS & CONFIG
# -------------------------
DEFAULT_MODEL = getattr(settings, "LLM_MODEL", "gemini-2.5-flash")

DB_SCHEMA_CONTEXT = """
PostgreSQL schema (read-only). Use user_id = $1 for scoping.
Tables:
- trades(user_id, symbol, direction, status, pnl, entry_price, exit_price, fees, entry_time, instrument_type, strategy_id, tags)
- strategies(id, user_id, name, style, description)
"""

CORE_METRICS = {
    "pnl", "profit", "loss", "win rate", "winrate",
    "trades", "performance", "returns"
}

KEYWORDS_DATA = {
    "win rate", "pnl", "profit", "loss", "trades",
    "avg", "average", "sum", "total",
    "count", "how many", "best strategy",
    "worst strategy", "performance",
    "drawdown", "expectancy"
}

KEYWORDS_REASONING = {
    "how should", "how to", "advice",
    "improve", "why", "explain",
    "suggest", "should i"
}

# -------------------------
# SAFE JSON EXTRACTION
# -------------------------
_CODE_FENCE_RE = re.compile(r"(?s)```(?:\w+)?\n(.*?)```")
_LEADING_DATA_RE = re.compile(r"(?m)^\s*data:\s*", flags=re.IGNORECASE)

def _strip_code_fences_and_data_prefixes(text: str) -> str:
    if not text:
        return ""
    t = _CODE_FENCE_RE.sub(r"\1", text)
    t = _LEADING_DATA_RE.sub("", t)
    return t.strip()

def extract_json_object(text: str) -> Dict[str, Any]:
    if not text or not text.strip():
        raise ValueError("Empty LLM response")

    s = _strip_code_fences_and_data_prefixes(text)

    if s.startswith("{") and s.endswith("}"):
        return json.loads(s)

    start = s.find("{")
    if start == -1:
        raise ValueError("No JSON object found")

    depth = 0
    for i in range(start, len(s)):
        ch = s[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = s[start : i + 1]
                return json.loads(candidate)

    raise ValueError("Unbalanced JSON braces in LLM response")

# -------------------------
# SQL Validation
# -------------------------
FORBIDDEN_SQL_PATTERNS = [
    r";", 
    r"\bINSERT\b", r"\bUPDATE\b", r"\bDELETE\b",
    r"\bDROP\b", r"\bALTER\b", r"\bTRUNCATE\b", r"\bCREATE\b",
    r"\bEXEC\b", r"\bCALL\b", r"\bGRANT\b", r"\bREVOKE\b"
]

def validate_sql(sql: str) -> bool:
    if not sql or not isinstance(sql, str):
        return False

    normalized = sql.strip().lower()

    if not (normalized.startswith("select") or normalized.startswith("with")):
        return False

    for pat in FORBIDDEN_SQL_PATTERNS:
        if re.search(pat, normalized, re.IGNORECASE):
            return False

    # Strict scoping check
    if "user_id = $1" not in normalized and "user_id=$1" not in normalized:
        return False

    return True

# -------------------------
# Token estimation + Safe LLM call
# -------------------------
def estimate_tokens_from_messages(messages: List[Dict[str, str]]) -> int:
    text = " ".join(m.get("content", "") for m in messages)
    return max(1, int(len(text) / 4))

async def llm_safe_call(
    user_profile: Dict[str, Any],  # ✅ Accept Profile (No DB Fetch)
    messages: List[Dict[str, str]],
    model: str,
    request_id: str,
    estimated_output_ratio: float = 1.0,
) -> Dict[str, Any]:
    """
    Reserve tokens, call LLM, then commit usage. 
    Handles rollback for both DB and Cache if LLM fails.
    """
    user_id = user_profile["user_id"]
    
    # 1. Estimate
    input_tokens = estimate_tokens_from_messages(messages)
    estimated_output = int(input_tokens * estimated_output_ratio) + 10
    estimated_total = input_tokens + estimated_output

    # 2. Reserve tokens (Uses QuotaManager's efficient logic)
    await QuotaManager.reserve_ai_tokens(user_id, user_profile, estimated_total)

    try:
        # 3. Call Provider
        resp = await llm_client.generate_response(messages, model=model)
        content = resp.get("content", "")
        actual_output_tokens = max(0, int(len(content) / 4))

        # 4. Async Logging (Fire & Forget)
        try:
            await MetricsEngine.log_ai_usage(
                user_id=user_id,
                model=model,
                input_tokens=input_tokens,
                output_tokens=actual_output_tokens,
                latency_ms=0.0,
                provider=resp.get("provider", getattr(llm_client, "last_provider", "unknown")),
                context="chat_pipeline",
            )
        except Exception:
            pass

        return resp

    except Exception as e:
        logger.exception("LLM call failed; attempting rollback", extra={"request_id": request_id, "user_id": user_id})

        # 5. Rollback Logic (DB + Cache)
        # We must restore the tokens to prevent unfair blocking.
        try:
            new_val = await db.fetch_val(
                """
                UPDATE public.user_profiles
                SET monthly_ai_tokens_used = GREATEST(0, monthly_ai_tokens_used - $2)
                WHERE id = $1
                RETURNING monthly_ai_tokens_used
                """,
                user_id,
                estimated_total,
            )
            
            # ✅ Sync Cache so the user isn't blocked in RAM
            if new_val is not None:
                update_user_cache(user_id, {"monthly_ai_tokens_used": new_val})
                logger.info("Rolled back reserved tokens for user %s", user_id)
                
        except Exception:
            logger.exception("Failed to rollback reserved tokens")

        raise

# -------------------------
# ChatPipeline
# -------------------------
class ChatPipeline:
    
    @staticmethod
    async def process(
        user_profile: Dict[str, Any],  # ✅ Changed from user_id to user_profile
        message: str, 
        history: List[Dict[str, str]]
    ) -> str:
        """
        Main pipeline entrypoint. 
        NOTE: Caller must pass the full 'user_profile' (current_user) to avoid N+1 DB lookups.
        """
        user_id = user_profile["user_id"]
        request_id = str(uuid.uuid4())
        
        # 1. Classify
        intent = await ChatPipeline._classify_intent(message)
        intent_type = intent.get("type", "GENERAL")
        intent_args = intent.get("args", {})

        context: Dict[str, Any] = {"status": "ok", "meta": {"row_count": 0}, "data": []}

        try:
            if intent_type == "STANDARD_METRICS":
                period = intent_args.get("period", "ALL_TIME")
                context = await ChatTools.get_standard_metrics(user_id, period)

            elif intent_type == "DATA_QUERY":
                needs = await ChatPipeline._decide_data_needed(message)
                if not needs:
                    context = {"status": "ok", "info": "No database query required."}
                else:
                    sql = await ChatPipeline._generate_sql(message)
                    if not sql or sql.upper() == "NO_SQL":
                        context = {"status": "error", "message": "Failed to generate SQL."}
                    else:
                        if not validate_sql(sql):
                            context = {"status": "error", "message": "Generated SQL failed safety checks."}
                        else:
                            context = await ChatTools.execute_secure_sql(user_id, sql)

            elif intent_type == "REASONING_ONLY":
                context = {"status": "ok", "info": "Reasoning-only response."}

            else:
                context = {"status": "ok", "info": "General conversation."}

        except Exception:
            logger.exception("ChatPipeline execution error", extra={"request_id": request_id, "user_id": user_id})
            context = {"status": "error", "message": "Internal processing error."}

        # 2. Synthesize
        return await ChatPipeline._synthesize_answer(user_profile, message, context, history, request_id=request_id)

    # -------------------------
    # INTENT CLASSIFICATION
    # -------------------------
    @staticmethod
    async def _classify_intent(message: str) -> Dict[str, Any]:
        text = message.lower().strip()

        if any(k in text for k in CORE_METRICS):
            if any(x in text for x in [" on ", " by ", " per ", " when ", " where ", " strategy", " tag", " symbol"]):
                return {"type": "DATA_QUERY"}
            return {"type": "STANDARD_METRICS", "args": {"period": "ALL_TIME"}}

        if text in {"hi", "hello", "hey"}:
            return {"type": "GENERAL"}

        prompt = """
        Classify the user request into exactly ONE of these types.
        Return JSON ONLY.

        TYPES:
        - STANDARD_METRICS
        - DATA_QUERY
        - REASONING_ONLY
        - GENERAL

        Return format:
        { "type": "...", "args": { } }
        """

        try:
            resp = await llm_client.generate_response(
                [{"role": "system", "content": prompt}, {"role": "user", "content": message}],
                model=DEFAULT_MODEL,
            )
            content = resp.get("content", "")
            try:
                return extract_json_object(content)
            except Exception:
                return {"type": "GENERAL"}
        except Exception:
            return {"type": "GENERAL"}

    # -------------------------
    # DATA-NEEDED DECISION
    # -------------------------
    @staticmethod
    async def _decide_data_needed(message: str) -> bool:
        t = message.lower()
        if any(k in t for k in KEYWORDS_DATA):
            return True
        if any(k in t for k in KEYWORDS_REASONING):
            return False

        prompt = f"Does this message require querying the user's database? Answer YES or NO only.\n\nMessage: \"{message}\""
        try:
            resp = await llm_client.generate_response(
                [{"role": "system", "content": "Answer YES or NO only."}, {"role": "user", "content": prompt}],
                model=DEFAULT_MODEL,
            )
            return resp.get("content", "").strip().upper().startswith("Y")
        except Exception:
            return True

    # -------------------------
    # SQL GENERATION
    # -------------------------
    @staticmethod
    async def _generate_sql(message: str) -> str:
        system_prompt = f"""
        You generate a single PostgreSQL SELECT (or WITH ... SELECT) query, RAW SQL only.
        SCHEMA: {DB_SCHEMA_CONTEXT}
        RULES:
        - Generate SQL ONLY if data is required. If not, return exactly NO_SQL.
        - Use only SELECT / WITH ... SELECT.
        - ALWAYS include `user_id = $1` in any WHERE clause.
        - Prefer aggregates (SUM, AVG, COUNT).
        - No destructive operations.
        """
        try:
            resp = await llm_client.generate_response(
                [{"role": "system", "content": system_prompt}, {"role": "user", "content": message}],
                model=DEFAULT_MODEL,
            )
            sql = resp.get("content", "").replace("```sql", "").replace("```", "").strip()
            return sql if sql else "NO_SQL"
        except Exception:
            return "NO_SQL"

    # -------------------------
    # SYNTHESIS (LLM for final answer)
    # -------------------------
    @staticmethod
    async def _synthesize_answer(
        user_profile: Dict[str, Any], # ✅ Accepts Profile
        message: str,
        context: Dict[str, Any],
        history: List[Dict[str, str]],
        request_id: Optional[str] = None,
    ) -> str:
        # Compact context
        safe_context = dict(context)
        if isinstance(safe_context.get("data"), list):
            safe_context["data"] = safe_context["data"][:15] # Limit rows

        if "preferences" in safe_context:
            safe_context["preferences"] = "[REDACTED]"

        system_prompt = f"""
        You are a concise, factual Trading Analyst. Use ONLY the provided context data to answer.
        CONTEXT:
        {json.dumps(safe_context, default=str)[:30_000]}
        RULES:
        - If status == 'error', explain the error.
        - If data is empty, say "I found no matching trades."
        - Round numeric values to 2 decimals.
        """
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(history[-2:] if history else [])
        messages.append({"role": "user", "content": message})

        try:
            # ✅ Use Safe Call with Profile (No extra DB fetch)
            resp = await llm_safe_call(
                user_profile=user_profile,
                messages=messages,
                model=DEFAULT_MODEL,
                request_id=request_id or str(uuid.uuid4()),
                estimated_output_ratio=1.0,
            )
            return resp.get("content", "I'm having trouble generating the answer right now.")
        except Exception:
            logger.exception("Synthesis failed", extra={"request_id": request_id, "user_id": user_profile["user_id"]})
            return "I'm having trouble generating the answer right now."