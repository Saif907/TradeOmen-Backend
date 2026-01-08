# backend/app/core/database.py

import asyncpg
import logging
import ssl
from contextlib import asynccontextmanager
from typing import Optional, Any

from tenacity import retry, stop_after_attempt, wait_fixed
from supabase import create_client, Client

from app.core.config import settings

logger = logging.getLogger("tradeomen.database")


class DatabaseConnectionError(RuntimeError):
    pass


class Database:
    """
    Industry-grade database manager.

    - asyncpg pool for high-performance queries (RLS BYPASS)
    - Supabase SDK for auth, storage, realtime
    """

    def __init__(self):
        self.pool: Optional[asyncpg.Pool] = None

        if settings.SUPABASE_URL and settings.SUPABASE_SERVICE_ROLE_KEY:
            self.supabase: Optional[Client] = create_client(
                settings.SUPABASE_URL,
                settings.SUPABASE_SERVICE_ROLE_KEY,
            )
        else:
            self.supabase = None
            logger.warning("Supabase client not initialized (missing credentials)")

    # -------------------------------------------------------------------------
    # Connection Lifecycle
    # -------------------------------------------------------------------------

    @retry(stop=stop_after_attempt(5), wait=wait_fixed(2))
    async def connect(self) -> None:
        if not settings.DATABASE_DSN:
            raise DatabaseConnectionError("DATABASE_DSN is not configured")

        logger.info("Initializing PostgreSQL connection pool")

        try:
            ssl_context = ssl.create_default_context()

            statement_cache_size = (
                0 if ":6543" in settings.DATABASE_DSN else 100
            )

            self.pool = await asyncpg.create_pool(
                dsn=settings.DATABASE_DSN,
                min_size=settings.MIN_CONNECTION_POOL_SIZE,
                max_size=settings.MAX_CONNECTION_POOL_SIZE,
                command_timeout=30,
                statement_cache_size=statement_cache_size,
                ssl=ssl_context,
            )

            async with self.pool.acquire() as conn:
                await conn.execute("SELECT 1")

            logger.info("Database pool ready")

        except Exception as exc:
            logger.critical("Database connection failed", exc_info=True)
            raise DatabaseConnectionError("Failed to connect to database") from exc

    async def disconnect(self) -> None:
        if self.pool:
            await self.pool.close()
            self.pool = None
            logger.info("Database pool closed")

    @property
    def is_connected(self) -> bool:
        return self.pool is not None and not self.pool._closed

    # -------------------------------------------------------------------------
    # Safety Guard
    # -------------------------------------------------------------------------

    def _require_pool(self) -> asyncpg.Pool:
        if not self.pool:
            raise DatabaseConnectionError("Database not connected")
        return self.pool

    # -------------------------------------------------------------------------
    # Query Helpers (RLS BYPASS — CALLERS MUST FILTER BY user_id)
    # -------------------------------------------------------------------------

    async def fetch_one(self, query: str, *args) -> Optional[asyncpg.Record]:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            return await conn.fetchrow(query, *args)

    async def fetch_all(self, query: str, *args) -> list[asyncpg.Record]:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            return await conn.fetch(query, *args)

    async def execute(self, query: str, *args) -> str:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            return await conn.execute(query, *args)

    # -------------------------------------------------------------------------
    # Transactions (CRITICAL FOR FINANCIAL DATA)
    # -------------------------------------------------------------------------

    @asynccontextmanager
    async def transaction(self):
        pool = self._require_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                yield conn
db = Database()