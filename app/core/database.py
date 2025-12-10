# backend/app/core/database.py
import asyncpg
import logging
import ssl
from tenacity import retry, stop_after_attempt, wait_fixed
from supabase import create_client, Client
from app.core.config import settings

logger = logging.getLogger(__name__)

class Database:
    """
    High-performance database manager implementing the Hybrid Architecture.
    
    1. self.pool (asyncpg): Direct PostgreSQL connection pool for high-speed I/O.
       WARNING: Bypasses RLS. You MUST manually filter by user_id in all queries.
       
    2. self.supabase (Client): Supabase SDK wrapper.
       Used for: Storage buckets, Auth management, Realtime subscriptions.
    """
    def __init__(self):
        self.pool: asyncpg.Pool | None = None
        # Initialize Supabase Client immediately (Stateless/HTTP)
        if settings.SUPABASE_URL and settings.SUPABASE_SERVICE_ROLE_KEY:
            self.supabase: Client = create_client(
                settings.SUPABASE_URL, 
                settings.SUPABASE_SERVICE_ROLE_KEY
            )
        else:
            logger.warning("⚠️ Supabase credentials missing. Client not initialized.")
            self.supabase = None

    @retry(stop=stop_after_attempt(5), wait=wait_fixed(2))
    async def connect(self):
        """
        Initializes the asyncpg connection pool on startup with robust retry logic.
        """
        try:
            logger.info("Connecting to Database via AsyncPG...")
            
            if not settings.DATABASE_DSN:
                raise ValueError("DATABASE_DSN is not set")

            # Create an SSL context that is compatible with cloud providers (Supabase)
            # This is often needed to verify the server certificate properly.
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

            # INTELLIGENT CONFIGURATION:
            # If using Supavisor (Port 6543), prepared statements can cause conflicts.
            # We disable the statement cache to ensure stability in Transaction Mode.
            statement_cache_size = 0 if ":6543" in settings.DATABASE_DSN else 100

            self.pool = await asyncpg.create_pool(
                dsn=settings.DATABASE_DSN,
                min_size=settings.MIN_CONNECTION_POOL_SIZE, # Keep this low (e.g., 5)
                max_size=settings.MAX_CONNECTION_POOL_SIZE, # Keep this conservative (e.g., 10-15)
                command_timeout=60,
                statement_cache_size=statement_cache_size,
                ssl=ctx
            )
            
            # Verify connection
            async with self.pool.acquire() as connection:
                await connection.execute("SELECT 1")
                
            logger.info("✅ Database Pool Established (AsyncPG).")
            
        except Exception as e:
            logger.critical(f"❌ Database Connection Failed: {e}")
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

    # --- Helper Methods (RLS BYPASS WARNING APPLIES) ---

    async def fetch_one(self, query: str, *args):
        if not self.pool: raise Exception("Database not connected")
        async with self.pool.acquire() as connection:
            return await connection.fetchrow(query, *args)

    async def fetch_all(self, query: str, *args):
        if not self.pool: raise Exception("Database not connected")
        async with self.pool.acquire() as connection:
            return await connection.fetch(query, *args)

    async def execute(self, query: str, *args):
        if not self.pool: raise Exception("Database not connected")
        async with self.pool.acquire() as connection:
            return await connection.execute(query, *args)

# Singleton Database Instance
db = Database()