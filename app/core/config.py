# backend/app/core/config.py
import os
from typing import List, Optional
from pydantic_settings import BaseSettings, SettingsConfigDict
from dotenv import load_dotenv

load_dotenv()

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file="../../.env",   # <-- FIXED PATH
        env_file_encoding="utf-8",
        extra="ignore"
    )

    APP_NAME: str = "TradeLM AI Backend"
    APP_VERSION: str = "0.1.0"
    ENVIRONMENT: str = "development"

    SERVER_HOST: str = "0.0.0.0"
    SERVER_PORT: int = 8000

    CORS_ALLOWED_ORIGINS: List[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    # Database DSN MUST be required
    DATABASE_DSN: str          # <-- REQUIRED, not optional

    MIN_CONNECTION_POOL_SIZE: int = 5
    MAX_CONNECTION_POOL_SIZE: int = 20

    # REMOVE default empty strings so .env is used
    SUPABASE_URL: str
    SUPABASE_SERVICE_ROLE_KEY: str

    OPENAI_API_KEY: Optional[str] = None
    GEMINI_API_KEY: Optional[str] = None

    LLM_MODEL: str = "gpt-4-turbo"
    LLM_PROVIDER: str = "openai"

settings = Settings()
