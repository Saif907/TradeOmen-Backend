from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import os

# Import configuration settings
# Note the relative import path adjustment since main.py is now outside the app directory
from app.libs.config import settings

# Import API Routers
from app.apis import auth, trades, strategies, ai_proxy


# 1. Initialize the FastAPI application
app = FastAPI(
    title="TradeLM Main Backend Service",
    description="Secure, Multi-tenant API for managing trades, strategies, and integrating the AI Microservice.",
    version="1.0.0",
)


# 2. Configure CORS Middleware
# This allows your React frontend (likely running on a different port) to communicate with the backend.
origins = [
    "http://localhost:3000",
    "http://localhost:5173",
    "http://127.0.0.1:3000",
    "http://localhost:8080",  
    "http://127.0.0.1:8080",  
    settings.AI_MICROSERVICE_URL,
    # Production origins can be added dynamically from environment variables
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# 3. Include API Routers
app.include_router(auth.router, prefix="/api/v1/auth", tags=["Auth & User Management"])
app.include_router(trades.router, prefix="/api/v1/trades", tags=["Trades"])
app.include_router(strategies.router, prefix="/api/v1/strategies", tags=["Strategies"])
app.include_router(ai_proxy.router, prefix="/api/v1/ai", tags=["AI Chat & Tagging"])


@app.get("/api/v1/health")
def health_check():
    """Simple health check endpoint to verify the service is running."""
    return {"status": "ok", "service": "TradeLM Backend"}