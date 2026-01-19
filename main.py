import logging
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from app.core.config import settings
from app.core.database import db
from app.core.exception import register_exception_handlers
from app.apis.v1 import api_router as api_v1_router

# ✅ NEW IMPORTS for Optimization & Monitoring
from app.core.middleware import APIMonitorMiddleware
from app.services.performance_monitor import PerformanceMonitor
from app.services.metrics_engine import MetricsEngine
from app.services.analytics import Analytics  # ✅ Analytics Import

# ✅ NEW IMPORTS for Rate Limiting
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from app.core.limiter import limiter

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
    # ------------------------------------
    # 1. Startup
    # ------------------------------------
    try:
        await db.connect()
        logger.info("✅ Database connected successfully")
        
        # 🚀 Start the "Zero-Noise" Performance Monitor in background
        asyncio.create_task(PerformanceMonitor.start_background_monitor())
        
        # 📊 Initialize Analytics (PostHog)
        # This checks for the API key and sets up the client
        Analytics.init()
        logger.info("✅ Analytics initialized")
        
    except Exception as e:
        logger.error(f"❌ Database connection failed: {e}")
    
    yield  # App runs here

    # ------------------------------------
    # 2. Shutdown
    # ------------------------------------
    logger.info("🛑 Shutting down...")
    
    # 💾 Force flush any buffered logs/metrics to DB
    await MetricsEngine.force_flush_all()
    
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
# Rate Limiting Setup
# --------------------------------------------------------------------------
# Register the limiter state and the 429 Exception Handler
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


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

# 3. Connectivity: CORS
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

# 4. ✅ MONITORING: API Latency & Error Tracking
# We add this LAST so it becomes the OUTERMOST layer.
# It will measure total time including CORS, GZip, and Validation.
app.add_middleware(APIMonitorMiddleware)


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