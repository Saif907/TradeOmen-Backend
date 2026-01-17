# backend/app/services/chat_tools.py

import logging
import re
from uuid import UUID
from typing import Dict, Any, List

from app.core.database import db

logger = logging.getLogger("tradeomen.chat_tools")

class ChatTools:
    """
    Final security boundary between LLM and database.
    This MUST be paranoid.
    """

    ALLOWED_TABLES = {"trades", "strategies"}
    ALLOWED_JOIN = "trades.strategy_id = strategies.id"

    MAX_ROWS = 30
    MIN_SAMPLE_SIZE = 2

    STATEMENT_TIMEOUT_MS = 3000  # 3 seconds hard cap

    FORBIDDEN_KEYWORDS = {
        "DROP", "DELETE", "UPDATE", "INSERT", "ALTER",
        "GRANT", "TRUNCATE", "EXECUTE", "CREATE",
        "PG_SLEEP", "pg_catalog", "information_schema",
        ";"
    }

    # -----------------------------
    # UUID SAFETY
    # -----------------------------
    @staticmethod
    def _to_uuid(user_id: Any) -> UUID:
        """
        Robustly converts input to UUID.
        Handles strings, standard UUIDs, and asyncpg UUID objects.
        """
        try:
            if isinstance(user_id, UUID):
                return user_id
            return UUID(str(user_id))
        except Exception:
            raise ValueError(f"Invalid user_id format: {user_id}")

    # -----------------------------
    # STANDARD METRICS (SAFE PATH)
    # -----------------------------
    @staticmethod
    async def get_standard_metrics(user_id: Any, period: str = "ALL_TIME") -> Dict[str, Any]:
        uid = ChatTools._to_uuid(user_id)

        date_clause = "TRUE"
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

        # ✅ FIX: Pass 'uid' as a positional argument (no values=...)
        row = await db.fetch_one(sql, uid)

        if not row or row["total_trades"] == 0:
            return {"status": "ok", "meta": {"row_count": 0}, "data": []}

        total = row["total_trades"]
        win_rate = round((row["wins"] / total * 100), 1)

        return {
            "status": "ok",
            "meta": {"row_count": 1},
            "data": [{
                "period": period,
                "total_trades": total,
                "net_pnl": float(row["net_pnl"]),
                "avg_pnl": float(row["avg_pnl"]),
                "win_rate": win_rate
            }]
        }

    # -----------------------------
    # DYNAMIC SQL (STRICT)
    # -----------------------------
    @staticmethod
    async def execute_secure_sql(user_id: Any, sql: str) -> Dict[str, Any]:
        if not sql:
            raise ValueError("Empty SQL")

        normalized = sql.strip().upper()

        # 1️⃣ READ-ONLY
        if not (normalized.startswith("SELECT") or normalized.startswith("WITH")):
            raise ValueError("Only SELECT queries allowed")

        # 2️⃣ BLOCK KEYWORDS
        for kw in ChatTools.FORBIDDEN_KEYWORDS:
            if re.search(rf"\b{kw}\b", normalized):
                raise ValueError("Forbidden SQL detected")

        # 3️⃣ USER SCOPE
        if "USER_ID = $1" not in normalized:
            raise ValueError("Missing user scope")

        # 4️⃣ TABLE ALLOWLIST (STRICT)
        for tbl in ChatTools.ALLOWED_TABLES:
            normalized = normalized.replace(f"FROM {tbl}", "")
            normalized = normalized.replace(f"JOIN {tbl}", "")

        if re.search(r"\bFROM\b|\bJOIN\b", normalized):
            raise ValueError("Unauthorized table access")

        # 5️⃣ JOIN CONDITION ENFORCEMENT
        if "JOIN STRATEGIES" in sql.upper():
            if ChatTools.ALLOWED_JOIN not in sql:
                raise ValueError("Invalid JOIN condition")

        # 6️⃣ FORCE LIMIT
        if "LIMIT" not in sql.upper():
            sql = f"{sql} LIMIT {ChatTools.MAX_ROWS}"

        uid = ChatTools._to_uuid(user_id)

        try:
            # ✅ FIX: Use db.transaction() to get the asyncpg connection 'conn'
            async with db.transaction() as conn:
                await conn.execute(
                    f"SET LOCAL statement_timeout = {ChatTools.STATEMENT_TIMEOUT_MS}"
                )
                
                # ✅ FIX: Use 'conn.fetch' (asyncpg) and positional args
                rows = await conn.fetch(sql, uid)

        except Exception as e:
            logger.exception("Secure SQL execution failed")
            raise RuntimeError(f"Query execution failed: {str(e)}")

        data = [dict(r) for r in rows]

        return {
            "status": "ok",
            "meta": {
                "row_count": len(data),
                "truncated": len(data) >= ChatTools.MAX_ROWS,
                "insufficient_data": len(data) < ChatTools.MIN_SAMPLE_SIZE
            },
            "data": data
        }