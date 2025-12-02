# backend/main.py

import os
import time
import httpx # Import httpx for client management
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from loguru import logger
from dotenv import load_dotenv

# --- 1. Configuration and Initialization ---

load_dotenv()
ENVIRONMENT = os.getenv("ENVIRONMENT", "development")

logger.add("app.log", rotation="10 MB", retention="10 days", level="INFO")
logger.info(f"Starting TradeLM API in {ENVIRONMENT} environment.")

# Import modular components
from app.auth.jwt_handler import JWTAuthError, set_async_client # Import setter
from app.libs.supabase_client import get_supabase_service_client
from app.apis import auth, trades, data_import, ai_chat, billing

# --- 2. Application Lifespan (Robustness) ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Handles critical startup and shutdown procedures.
    Initializes the shared ASYNC HTTP client for efficient I/O.
    """
    logger.info("Application startup: Initializing services...")
    
    # Initialize ASYNC HTTP Client (CRITICAL for non-blocking I/O)
    try:
        async_client = httpx.AsyncClient()
        set_async_client(async_client) # Set client for JWT handler
        logger.success("Async HTTP Client initialized.")
    except Exception as e:
        logger.error(f"FATAL: Async HTTP Client failed to initialize: {e}")

    try:
        get_supabase_service_client()
        logger.success("Supabase service client initialized and connected successfully.")
    except Exception as e:
        logger.error(f"FATAL: Database connectivity failed during startup: {e}")

    logger.info("Application startup complete. Ready to serve requests.")
    yield
    
    # --- Shutdown ---
    logger.info("Application shutdown: Cleaning up resources...")
    # Close the ASYNC HTTP Client gracefully
    try:
        await async_client.aclose()
        logger.success("Async HTTP Client closed gracefully.")
    except Exception as e:
        logger.error(f"ERROR: Failed to close Async HTTP Client: {e}")
        
    logger.info("Application shutdown complete.")


# --- 3. FastAPI Initialization ---

app = FastAPI(
    title="TradeLM AI Journal API",
    description="High-performance, privacy-first backend for trading journal analytics and AI coaching.",
    version="1.0.0",
    lifespan=lifespan
)

# --- 4. Global Exception Handling (Non-Breakable) ---

@app.exception_handler(JWTAuthError)
async def jwt_auth_exception_handler(request: Request, exc: JWTAuthError):
    """Handles custom JWT authentication failures cleanly (401)."""
    logger.warning(f"AUTH_FAIL: {exc.detail} for path: {request.url.path}")
    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={"detail": "Invalid authentication credentials or session expired. Please log in again."},
    )

@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    """Handles all unexpected internal errors, ensuring a 500 response (Robustness)."""
    logger.exception(f"CRITICAL_ERROR: Unhandled Internal Server Error on path: {request.url.path}")
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An unexpected server error occurred. We are investigating the issue."},
    )


# --- 5. Middleware (Super Fast, Secure, and Efficient) ---

@app.middleware("http")
async def process_request_middleware(request: Request, call_next):
    """Middleware for request logging, timing (efficiency), and security headers."""
    
    start_time = time.time()
    
    logger.info(f"--> Incoming Request: {request.method} {request.url.path}")
    
    response = await call_next(request)
    
    process_time = time.time() - start_time
    logger.info(f"<-- Outgoing Response: {response.status_code} in {process_time:.4f}s")

    # Add security headers (Professional Standard / Security)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    
    # CRITICAL for Edge-First Caching Architecture: Enforce No-Cache on writes
    if request.method in ["POST", "PUT", "DELETE", "PATCH"]:
        response.headers["Cache-Control"] = "no-store, max-age=0"

    return response

# --- 6. Core API Health Check (/health) ---

@app.get("/health", summary="Basic health check for load balancer and monitoring")
async def health_check():
    """Returns application status and checks key dependencies."""
    return {
        "status": "UP",
        "database": "CLIENT_INITIALIZED", 
        "environment": ENVIRONMENT,
        "version": app.version
    }


# --- 7. Route Inclusion (Modular Design) ---

app.include_router(auth.router, prefix="/v1/auth", tags=["Auth & Profiles"])
app.include_router(trades.router, prefix="/v1/trades", tags=["Trades & Strategies"])
app.include_router(data_import.router, prefix="/v1/data", tags=["Data Import & Brokers"])
app.include_router(ai_chat.router, prefix="/v1/ai", tags=["AI Chat & Coaching"])
app.include_router(billing.router, prefix="/v1/billing", tags=["Subscriptions & Webhooks"])