# backend/app/libs/task_queue.py

import os
import httpx
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Any
from uuid import UUID
from loguru import logger
from anyio import to_thread
from dotenv import load_dotenv

load_dotenv()

# --- Configuration (Non-Blocking) ---
AI_SERVICE_API_KEY = os.getenv("AI_SERVICE_API_KEY")
AI_MICROSERVICE_URL = os.getenv("AI_MICROSERVICE_URL", "http://ai-microservice:8001")
# Vercel endpoint for cache invalidation (Edge-First Architecture)
CACHE_INVALIDATION_URL = os.getenv("CACHE_INVALIDATION_URL", "http://localhost:3000/api/revalidate") 


# --- ThreadPoolExecutor Initialization (Robustness/Efficiency on Free Tier) ---

# Use concurrency.futures for background tasks instead of a dedicated broker (Celery/RQ).
MAX_WORKERS = int(os.getenv("TASK_QUEUE_WORKERS", 4))
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
logger.info(f"TaskQueue Initialized: Using ThreadPoolExecutor with max {MAX_WORKERS} workers.")


# --- Internal HTTP Client for Asynchronous Operations ---

INTERNAL_HTTP_CLIENT = httpx.Client(
    base_url=AI_MICROSERVICE_URL,
    headers={"X-API-Key": AI_SERVICE_API_KEY},
    timeout=30.0 # High timeout is required for long AI computations
)


# --- Public API for Task Delegation (Super Fast & Reusable) ---

async def enqueue_task(task_name: str, payload: Dict[str, Any]) -> UUID:
    """
    (Super Fast) Submits a long-running task to the local thread pool, ensuring 
    the FastAPI event loop remains non-blocking.
    """
    job_id = UUID(int=int(time.time() * 1000000)) # Unique ID for tracking
    
    # Use anyio.to_thread to run the synchronous _process_task function in a separate thread.
    await to_thread(_process_task, job_id, task_name, payload)

    logger.info(f"Task '{task_name}' submitted to thread pool. Job ID: {job_id}")
    return job_id


async def _invalidate_edge_cache_async(payload: Dict[str, Any]):
    """
    (Super Fast / Robustness) Triggers the synchronous cache invalidation in a separate thread.
    """
    await to_thread(_invalidate_edge_cache, payload)
    
def _invalidate_edge_cache(payload: Dict[str, Any]):
    """
    Sends a request to Vercel/Edge endpoint to purge specific cached data.
    CRITICAL for global consistency and speed (Edge-First Architecture).
    """
    path = payload.get('cache_path')
    if not path:
        logger.error("CACHE_ERROR: Invalidation request missing 'cache_path'.")
        return
        
    logger.info(f"  > Triggering Edge Cache Invalidation for path: {path}")
    try:
        # Use httpx.post with the full invalidation URL
        response = httpx.post(
            CACHE_INVALIDATION_URL,
            json={"path": path},
            headers={"X-API-Key": AI_SERVICE_API_KEY}, # Use AI key for internal verification
            timeout=5
        )
        response.raise_for_status()
        logger.success(f"  > Cache invalidation successful for {path}.")
    except httpx.HTTPError as e:
        logger.error(f"  > CACHE FAILURE (HTTP {response.status_code}): Could not invalidate cache for {path}: {e}")
    except Exception as e:
        logger.error(f"  > CACHE FAILURE: Unexpected error during cache invalidation for {path}: {e}")


# --- Worker Simulation (Synchronous - Runs in a separate thread) ---

def _process_task(job_id: UUID, task_name: str, payload: Dict[str, Any]):
    """
    (Modular) Synchronous worker function that simulates calling the AI Microservice.
    """
    logger.info(f"[WORKER] Starting job {job_id} for task: {task_name}")

    try:
        # Simulate worker processing by routing based on task name
        if task_name in ["trade_analysis", "llm_response_generate", "process_import", "global_aggregation"]:
            
            # Use httpx.post to forward the request to the dedicated AI Microservice
            endpoint = task_name.replace("_", "-") # e.g., 'trade_analysis' -> '/trade-analysis'
            
            response = INTERNAL_HTTP_CLIENT.post(f"/{endpoint}", json=payload)
            response.raise_for_status()
            
            logger.success(f"[WORKER] AI Microservice call successful for task {task_name}.")
            # NOTE: The worker simulation needs to update Supabase after successful AI processing
            # (e.g., updating the trade with tags, or the import job status). This update logic
            # would be in the AI Microservice code (which we will write next).
            
        else:
            logger.warning(f"[WORKER] Unknown task name received: {task_name}")
            
        logger.success(f"[WORKER] Job {job_id} completed successfully.")
        
    except httpx.HTTPStatusError as e:
        logger.error(f"[WORKER] AI Service failed for task {task_name} (Status {e.response.status_code}): {e.response.text}")
    except Exception as e:
        logger.error(f"[WORKER] Job {job_id} failed for task {task_name}: {e}")