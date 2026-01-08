# backend/app/services/chat_pipeline.py
import logging
import json
import re
import uuid
from typing import Dict, Any, List, Optional

from app.lib.llm_client import llm_client
from app.services.chat_tools import ChatTools
from app.services.quota_manager import QuotaManager
from app.services.metrics_engine import MetricsEngine
from app.core.database import db

logger = logging.getLogger("tradeomen.chat_pipeline")


# -------------------------
# SCHEMA CONTEXT
# -------------------------
DB_SCHEMA_CONTEXT = """
PostgreSQL schema (read-only). Use user_id = $1 for scoping.

Tables:
- trades(user_id, symbol, direction, status, pnl, entry_price, exit_price, fees, entry_time, instrument_type, strategy_id, tags)
- strategies(id, user_id, name, style, description)
"""


# -------------------------
# KEYWORDS
# -------------------------
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
# SAFE JSON EXTRACTION (NO RECURSIVE REGEX)
# -------------------------
_CODE_FENCE_RE = re.compile(r"(?s)```(?:\w+)?\n(.*?)```")
_LEADING_DATA_RE = re.compile(r"(?m)^\s*data:\s*", flags=re.IGNORECASE)


def _strip_code_fences_and_data_prefixes(text: str) -> str:
    if not text:
        return ""
    # remove triple-backtick fences
    t = _CODE_FENCE_RE.sub(r"\1", text)
    # remove common "data: " prefixes (from streaming)
    t = _LEADING_DATA_RE.sub("", t)
    return t.strip()


def extract_json_object(text: str) -> Dict[str, Any]:
    """
    Safely extract the first valid JSON object from the LLM response.
    Scans for a balanced {...} substring rather than using recursive regex.
    Raises ValueError if none found or JSON decode fails.
    """
    if not text or not text.strip():
        raise ValueError("Empty LLM response")

    s = _strip_code_fences_and_data_prefixes(text)

    # Fast path: the whole response is JSON
    if s.startswith("{") and s.endswith("}"):
        return json.loads(s)

    # Find first '{'
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
# SQL Validation Helpers
# -------------------------
FORBIDDEN_SQL_PATTERNS = [
    r";",                         # stacked queries or trailing semicolons
    r"\bINSERT\b", r"\bUPDATE\b", r"\bDELETE\b",
    r"\bDROP\b", r"\bALTER\b", r"\bTRUNCATE\b", r"\bCREATE\b",
    r"\bEXEC\b", r"\bCALL\b", r"\bGRANT\b", r"\bREVOKE\b"
]


def validate_sql(sql: str) -> bool:
    """
    Conservative SQL validator. Returns True if SQL is allowed to execute.
    - Must start with SELECT or WITH.
    - Must include user_id = $1 (scoping).
    - Must not contain forbidden patterns.
    """
    if not sql or not isinstance(sql, str):
        return False

    normalized = sql.strip().lower()

    if not (normalized.startswith("select") or normalized.startswith("with")):
        return False

    for pat in FORBIDDEN_SQL_PATTERNS:
        if re.search(pat, normalized, re.IGNORECASE):
            return False

    if "user_id = $1" not in normalized and "user_id=$1" not in normalized:
        return False

    return True


# -------------------------
# Token estimation + safe LLM call (with conservative rollback)
# -------------------------
def estimate_tokens_from_messages(messages: List[Dict[str, str]]) -> int:
    """
    Heuristic: ~1 token per 4 characters (conservative).
    """
    text = " ".join(m.get("content", "") for m in messages)
    return max(1, int(len(text) / 4))


async def llm_safe_call(
    user_id: str,
    messages: List[Dict[str, str]],
    model: str,
    request_id: str,
    estimated_output_ratio: float = 1.0,
) -> Dict[str, Any]:
    """
    Reserve tokens, call LLM, then commit usage. If LLM call fails, attempt a conservative rollback
    by decrementing the previously reserved token count in the DB (best-effort).
    """
    # estimate
    input_tokens = estimate_tokens_from_messages(messages)
    estimated_output = int(input_tokens * estimated_output_ratio) + 10
    estimated_total = input_tokens + estimated_output

    # Load fresh user_profile (for atomic reservation)
    user_profile = {}
    try:
        user_profile = await db.fetch_one(
            "SELECT id, plan_tier, monthly_ai_tokens_used, quota_reset_at FROM public.user_profiles WHERE id = $1",
            user_id,
        ) or {}
    except Exception:
        # proceed with empty profile if DB read fails (quota_manager will treat it defensively)
        logger.debug("Could not load user profile for token reservation; proceeding with fallback")

    # Reserve tokens (atomic). This raises if insufficient tokens.
    await QuotaManager.reserve_ai_tokens(user_id, user_profile, estimated_total)

    try:
        # Call provider
        resp = await llm_client.generate_response(messages, model=model)
        content = resp.get("content", "")
        actual_output_tokens = max(0, int(len(content) / 4))

        # Best-effort log of actual usage
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
            logger.debug("Metrics logging failed (non-fatal)")

        return resp

    except Exception as e:
        logger.exception("LLM call failed; attempting to rollback reserved tokens", extra={"request_id": request_id, "user_id": user_id})

        # Best-effort rollback: decrement monthly_ai_tokens_used by estimated_total,
        # only if monthly_ai_tokens_used >= estimated_total (avoid negative counts).
        try:
            await db.execute(
                """
                UPDATE public.user_profiles
                SET monthly_ai_tokens_used = monthly_ai_tokens_used - $2
                WHERE id = $1 AND monthly_ai_tokens_used >= $2
                """,
                user_id,
                estimated_total,
            )
            logger.info("Rolled back reserved tokens (best-effort) for user %s", user_id)
        except Exception:
            logger.exception("Failed to rollback reserved tokens (best-effort)")

        raise


# -------------------------
# ChatPipeline
# -------------------------
class ChatPipeline:
    """
    Orchestrates safe intent classification, optional SQL generation & execution,
    and LLM synthesis. Backend rules are authoritative.
    """

    @staticmethod
    async def process(user_id: str, message: str, history: List[Dict[str, str]]) -> str:
        request_id = str(uuid.uuid4())
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

        return await ChatPipeline._synthesize_answer(user_id, message, context, history, request_id=request_id)

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
                model="gemini-2.5-flash",
            )
            content = resp.get("content", "")
            try:
                return extract_json_object(content)
            except Exception:
                logger.debug("Intent LLM returned non-json; falling back to GENERAL")
                return {"type": "GENERAL"}
        except Exception:
            logger.warning("Intent classification failed", exc_info=True)
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

        # Conservative default: ask LLM but default to True on error
        prompt = f"Does this message require querying the user's database? Answer YES or NO only.\n\nMessage: \"{message}\""
        try:
            resp = await llm_client.generate_response(
                [{"role": "system", "content": "Answer YES or NO only."}, {"role": "user", "content": prompt}],
                model="gemini-2.5-flash",
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

SCHEMA:
{DB_SCHEMA_CONTEXT}

RULES (critical):
- Generate SQL ONLY if data is required. If not, return exactly NO_SQL.
- Use only SELECT / WITH ... SELECT.
- ALWAYS include `user_id = $1` in any WHERE clause.
- Prefer aggregates (SUM, AVG, COUNT).
- Do NOT include LIMIT (the caller will append it).
- Do not include destructive operations.
Return the SQL string only.
"""
        try:
            resp = await llm_client.generate_response(
                [{"role": "system", "content": system_prompt}, {"role": "user", "content": message}],
                model="gemini-2.5-flash",
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
        user_id: str,
        message: str,
        context: Dict[str, Any],
        history: List[Dict[str, str]],
        request_id: Optional[str] = None,
    ) -> str:
        # compact context to avoid huge payloads
        safe_context = dict(context)
        if isinstance(safe_context.get("data"), list):
            safe_context["data"] = safe_context["data"][:10]

        if "preferences" in safe_context:
            safe_context["preferences"] = "[REDACTED]"

        system_prompt = f"""
You are a concise, factual Trading Analyst. Use ONLY the provided context data to answer, be actionable and clear.

CONTEXT:
{json.dumps(safe_context, default=str)[:30_000]}
RULES:
- If context.status == 'error' -> apologize briefly and explain the error.
- If context.data is empty -> say "I found no matching trades."
- Never invent numbers not present in the context.
- Round numeric values to 2 decimals where applicable.
"""
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(history[-2:] if history else [])
        messages.append({"role": "user", "content": message})

        try:
            resp = await llm_safe_call(
                user_id=user_id,
                messages=messages,
                model="gemini-2.5-flash",
                request_id=request_id or str(uuid.uuid4()),
                estimated_output_ratio=1.0,
            )
            return resp.get("content", "I'm having trouble generating the answer right now.")
        except Exception:
            logger.exception("Synthesis failed", extra={"request_id": request_id, "user_id": user_id})
            return "I'm having trouble generating the answer right now."
