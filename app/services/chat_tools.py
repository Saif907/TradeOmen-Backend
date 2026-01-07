# backend/app/services/chat_tools.py
import logging
import re
from uuid import UUID
from typing import Dict, Any, List

from app.core.database import db

logger = logging.getLogger(__name__)

class ChatTools:
    """
    Secure execution layer between LLM and database.
    This layer enforces safety, limits, and handles Type conversions (UUID).
    """

    # -----------------------------
    # CONFIG (Explicit Whitelists)
    # -----------------------------
    ALLOWED_TABLES = {"trades", "strategies"}
    ALLOWED_JOIN = "trades.strategy_id = strategies.id"

    MAX_ROWS = 30
    MIN_SAMPLE_SIZE = 2

    # Block both standard SQL keywords and Postgres specific system calls
    FORBIDDEN_KEYWORDS = [
        "DROP", "DELETE", "UPDATE", "INSERT", "ALTER",
        "GRANT", "TRUNCATE", "EXECUTE", "CREATE", "PG_SLEEP",
        "pg_catalog", "information_schema"
    ]

    # -----------------------------
    # HELPER: Type Safety
    # -----------------------------
    @staticmethod
    def _to_uuid(user_id: str):
        """
        Crucial Helper: Converts string user_id back to UUID object for asyncpg.
        The Router sends strings (for JSON), but DB driver needs UUID objects.
        """
        if isinstance(user_id, str):
            try:
                return UUID(user_id)
            except ValueError:
                logger.error(f"Invalid UUID string received: {user_id}")
                # We return the string anyway to let the DB raise the specific error
                return user_id
        return user_id

    # -----------------------------
    # FAST LANE (Standard Metrics)
    # -----------------------------
    @staticmethod
    async def get_standard_metrics(user_id: str, period: str = "ALL_TIME") -> Dict[str, Any]:
        """
        Optimized aggregation for dashboards. 
        Zero LLM latency, pure SQL speed.
        """
        # 1. Convert to UUID
        uid_obj = ChatTools._to_uuid(user_id)
        params = [uid_obj]
        
        # 2. Build Query
        date_clause = "1=1"
        if period == "LAST_7_DAYS":
            date_clause = "entry_time >= NOW() - INTERVAL '7 days'"
        elif period == "THIS_MONTH":
            date_clause = "entry_time >= DATE_TRUNC('month', NOW())"
        elif period == "LAST_30_DAYS":
            date_clause = "entry_time >= NOW() - INTERVAL '30 days'"

        sql = f"""
            SELECT 
                COUNT(*) AS total_trades,
                COALESCE(SUM(pnl), 0) AS net_pnl,
                ROUND(AVG(pnl), 2) AS avg_pnl,
                COUNT(*) FILTER (WHERE pnl > 0) AS wins
            FROM trades
            WHERE user_id = $1
              AND status = 'CLOSED'
              AND {date_clause}
        """

        try:
            row = await db.fetch_one(sql, *params)

            if not row or row["total_trades"] == 0:
                return {
                    "status": "ok",
                    "meta": {"row_count": 0, "period": period},
                    "data": []
                }

            total = row["total_trades"]
            # Prevent division by zero
            win_rate = (row["wins"] / total * 100) if total > 0 else 0.0

            return {
                "status": "ok",
                "meta": {"row_count": 1, "period": period},
                "data": [{
                    "period": period,
                    "total_trades": total,
                    "net_pnl": float(row["net_pnl"]),
                    "avg_pnl": float(row["avg_pnl"]),
                    "win_rate": round(win_rate, 1)
                }]
            }

        except Exception as e:
            logger.error(f"Standard metrics error: {e}")
            return {"status": "error", "message": f"Failed to calculate metrics: {str(e)}"}

    # -----------------------------
    # FLEXIBLE LANE (Dynamic SQL)
    # -----------------------------
    @staticmethod
    async def execute_secure_sql(user_id: str, sql: str) -> Dict[str, Any]:
        """
        Executes LLM-generated SQL with strict guardrails and type safety.
        """
        if not sql:
            return {"status": "error", "message": "Empty SQL"}

        # 1. Normalize & Clean
        # Remove trailing semicolon to allow appending LIMIT safely
        sql = sql.strip().rstrip(";")
        normalized = sql.upper()

        # 2. Security Rule: Read-only only
        if not (normalized.startswith("SELECT") or normalized.startswith("WITH")):
            return {"status": "error", "message": "Only SELECT/WITH queries allowed"}

        # 3. Security Rule: Block forbidden keywords
        for keyword in ChatTools.FORBIDDEN_KEYWORDS:
            # Word boundary check (\b) prevents false positives like 'update_at'
            if re.search(rf"\b{keyword}\b", normalized):
                return {"status": "error", "message": f"Forbidden keyword detected: {keyword}"}

        # 4. Security Rule: Enforce user scope
        # We check for UPPER case variants since we normalized the string
        if "USER_ID = $1" not in normalized:
            return {"status": "error", "message": "Query must scope by user_id = $1"}

        # 5. Security Rule: Validate tables
        # Extracts table names after FROM or JOIN
        tables_found = set(re.findall(r"\bFROM\s+([a-zA-Z0-9_]+)|\bJOIN\s+([a-zA-Z0-9_]+)", normalized))
        # Flatten the list of tuples returned by findall
        flat_tables = {t for pair in tables_found for t in pair if t}

        if not flat_tables.issubset(ChatTools.ALLOWED_TABLES):
            return {"status": "error", "message": f"Unauthorized tables accessed. Allowed: {ChatTools.ALLOWED_TABLES}"}

        # 6. Enforce LIMIT to protect memory
        if "LIMIT" not in normalized:
            sql = f"{sql} LIMIT {ChatTools.MAX_ROWS}"

        try:
            # 7. Convert UUID for Execution
            uid_obj = ChatTools._to_uuid(user_id)
            
            # Execute
            rows = await db.fetch_all(sql, uid_obj)
            
            truncated = len(rows) >= ChatTools.MAX_ROWS
            data = [dict(r) for r in rows]

            return {
                "status": "ok",
                "meta": {
                    "row_count": len(data),
                    "truncated": truncated,
                    "insufficient_data": len(data) < ChatTools.MIN_SAMPLE_SIZE
                },
                "data": data
            }

        except Exception as e:
            logger.error(f"Secure SQL execution failed: {e} | SQL: {sql}")
            return {
                "status": "error",
                "message": f"Database execution failed: {str(e)}"
            }