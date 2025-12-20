# backend/app/worker/tasks.py
import asyncio
import logging
import contextvars
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Any
from functools import partial

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
        Waits for the result (non-blocking for the event loop).
        """
        loop = asyncio.get_running_loop()
        
        # Capture current context (DB sessions, Request IDs, etc.)
        ctx = contextvars.copy_context()
        
        # Wrap function to run inside the captured context
        func_with_context = partial(ctx.run, func, *args, **kwargs)
        
        return await loop.run_in_executor(self.executor, func_with_context)

    def submit_task(self, func: Callable, *args, **kwargs):
        """
        Fire-and-forget task submission.
        Does NOT wait for the result.
        Wraps execution to ensure errors are logged.
        """
        # Capture context
        ctx = contextvars.copy_context()

        def safe_wrapper():
            try:
                # Run actual function inside correct context
                ctx.run(func, *args, **kwargs)
            except Exception as e:
                logger.error(f"Background task failed: {str(e)}", exc_info=True)

        self.executor.submit(safe_wrapper)

    def shutdown(self):
        """Clean up threads on app exit."""
        logger.info("Shutting down BackgroundRunner...")
        self.executor.shutdown(wait=True)
        logger.info("BackgroundRunner shut down.")

# Singleton instance
runner = BackgroundRunner()