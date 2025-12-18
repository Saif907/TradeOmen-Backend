# backend/main.py
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
import time
import logging

from app.core.config import settings
from app.core.database import db  # ✅ Imported
from app.apis.v1 import api_router # ✅ Imported
from app.core.exception import (
    global_exception_handler, 
    http_exception_handler, 
    validation_exception_handler
)

# Setup Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Lifespan context manager for FastAPI.
    Handles startup and shutdown events efficiently.
    """
    # --- Startup ---
    logger.info(f"🚀 Starting {settings.APP_NAME} in {settings.ENVIRONMENT} mode...")
    
    # ✅ Initialize Database Connection
    if settings.DATABASE_DSN:
        await db.connect()
    
    # ✅ Print Routes for Debugging
    logger.info("🗺️  AVAILABLE ROUTES:")
    for route in app.routes:
        if hasattr(route, "methods"):
            logger.info(f"   {route.methods} {route.path}")
        else:
            logger.info(f"   {route.path}")
    
    yield
    
    # --- Shutdown ---
    logger.info("🛑 Shutting down application...")
    if settings.DATABASE_DSN:
        await db.disconnect()
    
    # Close LLM client if it exists
    try:
        from app.lib.llm_client import llm_client
        await llm_client.close()
    except ImportError:
        pass

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    lifespan=lifespan,
    docs_url="/docs" if settings.ENVIRONMENT != "production" else None,
    redoc_url=None
)

# --- Exception Handlers ---
app.add_exception_handler(Exception, global_exception_handler)
app.add_exception_handler(StarletteHTTPException, http_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)

# --- Middleware ---
@app.middleware("http")
async def add_process_time_header(request: Request, call_next):
    start_time = time.time()
    response = await call_next(request)
    process_time = time.time() - start_time
    response.headers["X-Process-Time"] = str(process_time)
    return response

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Router Registration ---
# ✅ THIS IS THE FIX: Include the API router
app.include_router(api_router, prefix="/api/v1")

# --- Core Endpoints ---
@app.get("/health", tags=["System"])
async def health_check():
    return {
        "status": "healthy",
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "environment": settings.ENVIRONMENT,
        "database_pool": "connected" if db.is_connected else "disconnected"
    }

@app.get("/", tags=["System"])
async def root():
    return {
        "message": "Welcome to TradeOmen AI API", 
        "docs": "/docs" if settings.ENVIRONMENT != "production" else "Hidden"
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=settings.SERVER_HOST, port=settings.SERVER_PORT, reload=True)