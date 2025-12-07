# backend/app/apis/v1/__init__.py
from fastapi import APIRouter
from app.apis.v1 import auth

# TODO: Uncomment these as we create the files in the next steps
from app.apis.v1 import trades
from app.apis.v1 import strategies
from app.apis.v1 import ai_chat

api_router = APIRouter()

# 1. Authentication Router (Ready)
api_router.include_router(auth.router, prefix="/auth", tags=["Auth"])

# 2. Trades Router (Next)
api_router.include_router(trades.router, prefix="/trades", tags=["Trades"])

# 3. Strategies Router (Future)
api_router.include_router(strategies.router, prefix="/strategies", tags=["Strategies"])

# 4. AI Chat Router (Future)
api_router.include_router(ai_chat.router, prefix="/chat", tags=["AI Chat"])