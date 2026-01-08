# backend/main.py

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import settings
from app.core.database import db
from app.apis.v1 import api_router
from app.core.exception import (
    global_exception_handler,
    http_exception_handler,
    validation_exception_handler,
)

# ------------------------------------------------------------------------------
# Logging Configuration
# ------------------------------------------------------------------------------

logging.basicConfig(
    level=settings.LOG_LEVEL,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("tradeomen.api")

# ------------------------------------------------------------------------------
# Application Lifespan
# ------------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application startup & shutdown lifecycle.
    Fail-fast philosophy.
    """
    logger.info(
        "Starting %s v%s [%s]",
        settings.APP_NAME,
        settings.APP_VERSION,
        settings.APP_ENV,
    )

    # --------------------
    # Startup
    # --------------------
    if settings.DATABASE_DSN:
        try:
            await db.connect()
            logger.info("Database connection established")
        except Exception:
            logger.critical("Database connection failed", exc_info=True)
            raise

    if not settings.IS_PROD:
        logger.debug("Registered API routes:")
        for route in app.routes:
            if hasattr(route, "methods"):
                logger.debug("%s %s", route.methods, route.path)

    yield

    # --------------------
    # Shutdown
    # --------------------
    logger.info("Shutting down application")

    try:
        if db.is_connected:
            await db.disconnect()
            logger.info("Database connection closed")
    except Exception:
        logger.error("Error during database shutdown", exc_info=True)

    try:
        from app.lib.llm_client import llm_client
        await llm_client.close()
        logger.info("LLM client closed")
    except ImportError:
        pass
    except Exception:
        logger.error("Error closing LLM client", exc_info=True)

# ------------------------------------------------------------------------------
# FastAPI App
# ------------------------------------------------------------------------------

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    lifespan=lifespan,
    docs_url="/docs" if not settings.IS_PROD else None,
    redoc_url=None,
)

# ------------------------------------------------------------------------------
# Exception Handlers
# ------------------------------------------------------------------------------

app.add_exception_handler(StarletteHTTPException, http_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.add_exception_handler(Exception, global_exception_handler)

# ------------------------------------------------------------------------------
# Middleware
# ------------------------------------------------------------------------------

@app.middleware("http")
async def request_timing_middleware(request: Request, call_next):
    start_time = time.perf_counter()
    response = await call_next(request)
    duration = time.perf_counter() - start_time
    response.headers["X-Process-Time"] = f"{duration:.6f}"
    return response


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

# ------------------------------------------------------------------------------
# API Routes
# ------------------------------------------------------------------------------

app.include_router(api_router, prefix="/api/v1")

# ------------------------------------------------------------------------------
# System Endpoints
# ------------------------------------------------------------------------------

@app.get("/health", tags=["System"])
async def health_check():
    return {
        "status": "ok",
        "service": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "environment": settings.APP_ENV,
        "database": "connected" if db.is_connected else "disconnected",
    }


@app.get("/", tags=["System"])
async def root():
    return {
        "message": "TradeOmen API",
        "docs": "/docs" if not settings.IS_PROD else None,
    }

# ------------------------------------------------------------------------------
# Local Dev Entrypoint
# ------------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=settings.SERVER_HOST,
        port=settings.SERVER_PORT,
        reload=not settings.IS_PROD,
        log_level=settings.LOG_LEVEL.lower(),
    )
