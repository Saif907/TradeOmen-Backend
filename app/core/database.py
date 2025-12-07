# backend/app/core/database.py
import asyncpg
from supabase import create_client, Client
from app.core.config import settings
import logging

logger = logging.getLogger(__name__)

class Database:
    """
    High-performance database manager implementing the Hybrid Architecture.
    
    1. self.pool (asyncpg): Direct PostgreSQL connection pool for high-speed I/O.
       Used for: Trade logging, Analytics queries, Batch operations.
       
    2. self.supabase (Client): Supabase SDK wrapper.
       Used for: Storage buckets, Auth management, Realtime subscriptions.
    """
    def __init__(self):
        self.pool: asyncpg.Pool | None = None
        # Initialize Supabase Client immediately (it's stateless/HTTP-based)
        if settings.SUPABASE_URL and settings.SUPABASE_SERVICE_ROLE_KEY:
            self.supabase: Client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)
        else:
            logger.warning("⚠️ Supabase credentials missing. Client not initialized.")
            self.supabase = None

    async def connect(self):
        """
        Initializes the asyncpg connection pool on startup.
        """
        try:
            logger.info("Connecting to Database via AsyncPG...")
            
            if not settings.DATABASE_DSN:
                raise ValueError("DATABASE_DSN is not set in .env file")

            # ✅ FIXED: Use the correct settings from config.py
            self.pool = await asyncpg.create_pool(
                dsn=settings.DATABASE_DSN,
                min_size=settings.MIN_CONNECTION_POOL_SIZE,
                max_size=settings.MAX_CONNECTION_POOL_SIZE,
                command_timeout=60
            )
            logger.info("✅ Database Pool Established.")
        except Exception as e:
            logger.error(f"❌ Database Connection Failed: {e}")
            raise e

    async def disconnect(self):
        """
        Gracefully closes connections on shutdown.
        """
        if self.pool:
            await self.pool.close()
            logger.info("🛑 Database Pool Closed.")

    @property
    def is_connected(self) -> bool:
        return self.pool is not None

    # --- Helper Methods for Raw SQL ---

    async def fetch_one(self, query: str, *args):
        """Helper for single row fetches using the pool"""
        if not self.pool: raise Exception("Database not connected")
        async with self.pool.acquire() as connection:
            return await connection.fetchrow(query, *args)

    async def fetch_all(self, query: str, *args):
        """Helper for multiple row fetches using the pool"""
        if not self.pool: raise Exception("Database not connected")
        async with self.pool.acquire() as connection:
            return await connection.fetch(query, *args)

    async def execute(self, query: str, *args):
        """Helper for execute/insert/update using the pool"""
        if not self.pool: raise Exception("Database not connected")
        async with self.pool.acquire() as connection:
            return await connection.execute(query, *args)

# Singleton Database Instance
db = Database()