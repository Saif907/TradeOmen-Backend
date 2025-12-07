# backend/app/worker/tasks.py
import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Any
import logging

from app.core.config import settings

logger = logging.getLogger(__name__)

class BackgroundRunner:
    """
    Manages a pool of threads for running heavy background tasks.
    Used for:
    - CSV Imports (Parsing & DB Inserts)
    - Batch AI Analysis
    - Image Processing (Screenshots)
    """
    def __init__(self):
        self.executor = ThreadPoolExecutor(
            max_workers=settings.MAX_WORKER_THREADS,
            thread_name_prefix="tradelm_worker"
        )
        logger.info(f"Initialized BackgroundRunner with {settings.MAX_WORKER_THREADS} threads.")

    async def run_in_background(self, func: Callable, *args, **kwargs) -> Any:
        """
        Runs a synchronous function in a separate thread.
        This is non-blocking for the FastAPI event loop.
        """
        loop = asyncio.get_running_loop()
        # run_in_executor expects a function and its arguments.
        # We use a lambda or functools.partial if kwargs are needed, 
        # but for simple *args this works directly.
        return await loop.run_in_executor(self.executor, lambda: func(*args, **kwargs))

    def submit_task(self, func: Callable, *args, **kwargs):
        """
        Fire-and-forget task submission. 
        Does not wait for the result. Useful for logging, emails, etc.
        """
        self.executor.submit(func, *args, **kwargs)

    def shutdown(self):
        """Clean up threads on app exit."""
        self.executor.shutdown(wait=True)
        logger.info("BackgroundRunner shut down.")

# Singleton instance
runner = BackgroundRunner()