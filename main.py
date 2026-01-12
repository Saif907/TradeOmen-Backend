import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from app.core.config import settings
from app.core.database import db
from app.core.exception import register_exception_handlers
from app.apis.v1 import api_router as api_v1_router 

# --------------------------------------------------------------------------
# Logging Setup
# --------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("tradeomen.main")


# --------------------------------------------------------------------------
# Lifespan (Startup/Shutdown)
# --------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Startup: Connect to DB
    try:
        await db.connect()
        logger.info("✅ Database connected successfully")
    except Exception as e:
        logger.error(f"❌ Database connection failed: {e}")
    
    yield  # App runs here

    # 2. Shutdown: Disconnect DB
    await db.disconnect()
    logger.info("🛑 Database disconnected")


# --------------------------------------------------------------------------
# App Initialization
# --------------------------------------------------------------------------
app = FastAPI(
    title=settings.PROJECT_NAME,
    version="1.0.0",
    description="Industry-grade SaaS Trading Journal Backend",
    lifespan=lifespan,
    docs_url="/docs" if settings.ENVIRONMENT != "production" else None,
    redoc_url="/redoc" if settings.ENVIRONMENT != "production" else None,
)


# --------------------------------------------------------------------------
# Middleware (Order Matters!)
# --------------------------------------------------------------------------

# 1. Security: Trusted Hosts
app.add_middleware(
    TrustedHostMiddleware, 
    allowed_hosts=["*"] if settings.ENVIRONMENT == "development" else ["tradeomen.com", "*.tradeomen.com", "localhost"]
)

# 2. Performance: GZip Compression
app.add_middleware(GZipMiddleware, minimum_size=1000)

# 3. Connectivity: CORS (Fixes Access-Control-Allow-Origin errors)
allow_origins = [
    "http://localhost:5173",      # Standard Vite
    "http://127.0.0.1:5173",
    "http://localhost:8080",      # YOUR FRONTEND PORT
    "http://127.0.0.1:8080",      # YOUR FRONTEND IP
    "http://localhost:3000",      # React/Next.js Default
    "https://app.tradeomen.com",  # Production
]

if hasattr(settings, "BACKEND_CORS_ORIGINS") and settings.BACKEND_CORS_ORIGINS:
    for origin in settings.BACKEND_CORS_ORIGINS:
        if str(origin) not in allow_origins:
            allow_origins.append(str(origin))

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=True,
    allow_methods=["*"], 
    allow_headers=["*"], 
)


# --------------------------------------------------------------------------
# Exception Handlers & Routers
# --------------------------------------------------------------------------
register_exception_handlers(app)
app.include_router(api_v1_router, prefix=settings.API_V1_STR)


# --------------------------------------------------------------------------
# Root Endpoint (Health Check)
# --------------------------------------------------------------------------
@app.get("/", tags=["Health"])
async def health_check():
    return {
        "status": "healthy",
        "database": "connected" if db.is_connected else "disconnected",
        "version": "1.0.0"
    }