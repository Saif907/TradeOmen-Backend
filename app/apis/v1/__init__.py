# backend/app/apis/v1/__init__.py

from fastapi import APIRouter
from app.core.config import settings

# Import all route modules
from app.apis.v1 import (
    auth,
    trades,
    strategies,
    brokers,
    news,
    metrics,
    admin 
)
from app.apis.v1.chat.router import router as chat_router

api_router = APIRouter()

# 1. Authentication Router
api_router.include_router(auth.router, prefix="/auth", tags=["Auth"])

# 2. Trades Router
api_router.include_router(trades.router, prefix="/trades", tags=["Trades"])

# 3. Strategies Router
api_router.include_router(strategies.router, prefix="/strategies", tags=["Strategies"])

# 4. AI Chat Router
api_router.include_router(chat_router, prefix="/chat", tags=["AI Chat"])

# 5. Brokers Router
api_router.include_router(brokers.router, prefix="/brokers", tags=["Brokers"])

# 6. News Router
api_router.include_router(news.router, prefix="/news", tags=["News"])

# 7. Metrics & Telemetry Router
api_router.include_router(metrics.router, prefix="/metrics", tags=["Metrics"])

# 8. ✅ FIXED: Admin Panel Router
# We manually set prefix="/admin" to match the Frontend's request path.
api_router.include_router(
    admin.router, 
    prefix="/admin",   # <--- Changed from settings.ADMIN_PANEL_PATH to "/admin"
    tags=["Admin Panel"]
)