# backend/app/core/config.py

import os
from typing import List, Optional, Dict, Any
from pydantic_settings import BaseSettings, SettingsConfigDict
from dotenv import load_dotenv

load_dotenv()

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True
    )

    # --- App Info ---
    APP_NAME: str = "TradeOmen AI Backend"
    APP_VERSION: str = "0.1.0"
    ENVIRONMENT: str = "development"
    LOG_LEVEL: str = "INFO"

    # --- Server ---
    SERVER_HOST: str = "0.0.0.0"
    SERVER_PORT: int = 8000
    
    # --- Frontend URL ---
    FRONTEND_URL: str = "http://localhost:8080"

    # --- CORS ---
    CORS_ALLOWED_ORIGINS: List[str] = [
        "http://localhost:8080",
        "http://127.0.0.1:8080",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://www.tradeomen.com",
        "https://app.tradeomen.com"
    ]

    # --- Database (Supabase / Postgres) ---
    DATABASE_DSN: str
    
    # Connection Pool Settings
    MIN_CONNECTION_POOL_SIZE: int = 5
    MAX_CONNECTION_POOL_SIZE: int = 20

    # --- Supabase (API & Auth) ---
    SUPABASE_URL: str
    SUPABASE_SERVICE_ROLE_KEY: str  # Admin
    SUPABASE_ANON_KEY: str          # Public

    # --- Storage ---
    SCREENSHOT_BUCKET: str = "trade-screenshots"
    MAX_UPLOAD_SIZE_BYTES: int = 5 * 1024 * 1024  # 5MB
    ALLOWED_IMAGE_TYPES: List[str] = ["image/png", "image/jpeg", "image/jpg", "image/webp"]

    # --- Security & Encryption ---
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ENCRYPTION_KEY: str

    # --- LLM (AI) Settings ---
    OPENAI_API_KEY: Optional[str] = None
    GEMINI_API_KEY: Optional[str] = None
    PERPLEXITY_API_KEY: Optional[str] = None

    LLM_PROVIDER: str = "gemini"
    LLM_MODEL: str = "gemini-1.5-flash"
    
    # RAG / Vectors
    EMBEDDING_PROVIDER: str = "openai"
    EMBEDDING_MODEL: str = "text-embedding-3-small"
    VECTOR_STORE_TABLE: str = "embeddings"

    MAX_WORKER_THREADS: int = 8

    # --- Plan Limits (Centralized Quotas) ---
    # ✅ ALIGNED: These keys match exactly what QuotaManager expects
    PLAN_LIMITS: Dict[str, Dict[str, Any]] = {
        "FREE": {
            "max_strategies": 1,
            "max_total_trades": 50,  # Total trades allowed in journal
            "daily_chat_msgs": 10,
            "monthly_csv_imports": 1,
            "allow_web_search": False,
            "allow_broker_sync": False,
            "allow_csv_export": False,
        },
        "PRO": {
            "max_strategies": 50,
            "max_total_trades": 100_000,
            "daily_chat_msgs": 500,
            "monthly_csv_imports": 100,
            "allow_web_search": True,
            "allow_broker_sync": True,
            "allow_csv_export": True,
        },
        "FOUNDER": {
            "max_strategies": 1_000,
            "max_total_trades": 1_000_000,
            "daily_chat_msgs": 1_000_000,
            "monthly_csv_imports": 1_000_000,
            "allow_web_search": True,
            "allow_broker_sync": True,
            "allow_csv_export": True,
        }
    }

    # --- Broker Integrations (Dhan) ---
    DHAN_CLIENT_ID: Optional[str] = None
    DHAN_CLIENT_SECRET: Optional[str] = None
    DHAN_REDIRECT_URI: str = "http://localhost:8000/api/v1/brokers/dhan/callback"

settings = Settings()