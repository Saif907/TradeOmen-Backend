# backend/app/services/chat_pipeline.py
import logging
import json
import re
from typing import Dict, Any, List

from app.lib.llm_client import llm_client
from app.services.chat_tools import ChatTools

logger = logging.getLogger(__name__)

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

KEYWORDS_DATA = [
    "win rate", "pnl", "profit", "loss", "trades",
    "avg", "average", "sum", "total",
    "count", "how many", "best strategy",
    "worst strategy", "performance",
    "drawdown", "expectancy"
]

KEYWORDS_REASONING = [
    "how should", "how to", "advice",
    "improve", "why", "explain",
    "suggest", "should i"
]


# -------------------------
# SAFE JSON PARSER (CRITICAL)
# -------------------------
def safe_json_parse(text: str) -> Dict[str, Any]:
    if not text or not text.strip():
        raise ValueError("Empty LLM response")

    text = text.strip()

    # Direct JSON
    if text.startswith("{"):
        return json.loads(text)

    # Extract JSON from text
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        return json.loads(match.group(0))

    raise ValueError("No JSON object found in response")


class ChatPipeline:
    """
    Orchestrates intent routing, SQL generation, execution,
    and final synthesis. Backend rules are authoritative.
    """

    # -------------------------
    # PUBLIC ENTRYPOINT
    # -------------------------
    @staticmethod
    async def process(user_id: str, message: str, history: List[Dict[str, str]]) -> str:
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
                        context = {"status": "error", "message": "Failed to generate safe SQL."}
                    else:
                        context = await ChatTools.execute_secure_sql(user_id, sql)

            elif intent_type == "REASONING_ONLY":
                context = {"status": "ok", "info": "Reasoning-only response."}

            else:
                context = {"status": "ok", "info": "General conversation."}

        except Exception:
            logger.exception("ChatPipeline execution error")
            context = {"status": "error", "message": "Internal processing error."}

        return await ChatPipeline._synthesize_answer(message, context, history)

    # -------------------------
    # INTENT CLASSIFICATION (FIXED)
    # -------------------------
    @staticmethod
    async def _classify_intent(message: str) -> Dict[str, Any]:
        text = message.lower().strip()

        # ---- HARD BACKEND RULES (NO LLM) ----
        if any(k in text for k in CORE_METRICS):
            # metric + filter → DATA_QUERY
            if any(x in text for x in [" on ", " by ", " per ", " when ", " where ", " strategy", " tag", " symbol"]):
                return {"type": "DATA_QUERY"}
            return {"type": "STANDARD_METRICS", "args": {"period": "ALL_TIME"}}

        if text in {"hi", "hello", "hey"}:
            return {"type": "GENERAL"}

        # ---- LLM FALLBACK ----
        prompt = """
Classify the user request into exactly ONE of these types.
Return JSON ONLY.

IMPORTANT RULE:
If a user asks for a metric (like PnL, win rate, total trades) WITHOUT specifying a time period,
ASSUME the period is ALL_TIME and classify as STANDARD_METRICS.

TYPES:

1) STANDARD_METRICS
- Questions asking for basic performance metrics such as:
  PnL, win rate, total trades, average PnL
- Applies ONLY to predefined periods:
  LAST_7_DAYS, THIS_MONTH, LAST_30_DAYS, ALL_TIME
- If no period is mentioned, use ALL_TIME by default
- These do NOT require advanced SQL
- If u think user is only asking for a metric and not any other thing just a simple trading metric , classify as this type ,strictly follow this rule
- Do not return STANDARD_METRICS if u think there is data query intent needed , strictly follow the below 4 types

Examples:
- "what's my pnl" → STANDARD_METRICS (ALL_TIME)
- "my win rate" → STANDARD_METRICS (ALL_TIME)
- "performance this month" → STANDARD_METRICS (THIS_MONTH)

2) DATA_QUERY
- Questions that require querying the database with filters, grouping, joins, or custom logic
- Examples:
  - "win rate on AAPL"
  - "best strategy"
  - "average pnl on Tuesdays"
  - "trades with FOMO tag"
- These REQUIRE generating SQL

3) REASONING_ONLY
- Advice, explanations, psychology, or strategy guidance
- NO database query needed

Examples:
- "how can I improve my trading?"
- "why do I overtrade?"

4) GENERAL
- Greetings, casual chat, or non-trading questions
- Do not return General if u think there is trading metric or data intent related to trading , strictly follow the above types

Examples:
- "hi"
- "who are you?"

Return format:
{ "type": "...", "args": { ... } }
"""


        try:
            resp = await llm_client.generate_response(
                [{"role": "system", "content": prompt},
                 {"role": "user", "content": message}],
                model="gemini-2.5-flash",
                response_format={"type": "json_object"}
            )

            logger.debug("Raw intent LLM response: %r", resp.get("content"))

            intent = safe_json_parse(resp["content"])

            # ---- POST-VALIDATION ----
            if intent.get("type") == "GENERAL" and any(k in text for k in CORE_METRICS):
                return {"type": "STANDARD_METRICS", "args": {"period": "ALL_TIME"}}

            return intent

        except Exception as e:
            logger.warning(
                "Intent classification failed. Raw=%r Error=%s",
                resp.get("content") if "resp" in locals() else None,
                e
            )
            return {"type": "GENERAL"}

    # -------------------------
    # DATA NEEDED DECISION
    # -------------------------
    @staticmethod
    async def _decide_data_needed(message: str) -> bool:
        text = message.lower()

        if any(k in text for k in KEYWORDS_DATA):
            return True
        if any(k in text for k in KEYWORDS_REASONING):
            return False

        prompt = f"""
Does this message require querying the user's database?
Answer YES or NO only.

Message: \"{message}\"
"""
        try:
            resp = await llm_client.generate_response(
                [{"role": "system", "content": "Answer YES or NO only."},
                 {"role": "user", "content": prompt}],
                model="gemini-2.5-flash"
            )
            return resp["content"].strip().upper().startswith("Y")
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
- Use only SELECT/ WITH ... SELECT.
- ALWAYS include `user_id = $1` in any WHERE clause.
- Prefer aggregates (SUM, AVG, COUNT) instead of dumping raw rows.
- If user says "best" or "worst" and it's ambiguous, default to ranking by SUM(pnl) (total profit).
- Do NOT include LIMIT (the system will add it).
- Do not include any forbidden / destructive operations.

Return the SQL string only.
"""

        try:
            resp = await llm_client.generate_response(
                [{"role": "system", "content": system_prompt},
                 {"role": "user", "content": message}],
                model="gemini-2.5-flash"
            )
            sql = resp["content"].replace("```sql", "").replace("```", "").strip()
            return sql if sql else "NO_SQL"
        except Exception:
            return "NO_SQL"

    # -------------------------
    # FINAL SYNTHESIS
    # -------------------------
    @staticmethod
    async def _synthesize_answer(message: str, context: Dict[str, Any], history: List[Dict[str, str]]) -> str:
        system_prompt = f"""
You are a concise, factual Trading Analyst. Use ONLY the provided context data to answer.
You give answers more structured than plain text, with bullet points or numbered lists where applicable. A detailed answer is preferred.
Give space between paragraphs and points for readability.
Give precise numbers from the context.
Always motivate the user with insights based on the data. so that they can take action on it.
U need to engage the user.

CONTEXT (authoritative):
{json.dumps(context, indent=2)}

RULES:
- If context.status == 'error' -> apologize briefly and explain the error message.
- If context.meta.insufficient_data == true -> say data is insufficient and avoid firm conclusions.
- If context.data is empty -> say "I found no matching trades."
- Never invent numbers or facts not present in the context.
- Round numeric values to 2 decimal places where applicable.
- Keep the answer concise (1-6 sentences).
"""

        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(history[-3:] if history else [])
        messages.append({"role": "user", "content": message})

        try:
            resp = await llm_client.generate_response(messages, model="gemini-2.5-flash")
            return resp["content"]
        except Exception:
            return "I'm having trouble generating the answer right now."
