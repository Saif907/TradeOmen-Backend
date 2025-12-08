# backend/app/core/config.py
import os
from typing import List, Optional
from pydantic_settings import BaseSettings, SettingsConfigDict
from dotenv import load_dotenv

load_dotenv()

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file="../../.env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    APP_NAME: str = "TradeLM AI Backend"
    APP_VERSION: str = "0.1.0"
    ENVIRONMENT: str = "development"

    SERVER_HOST: str = "0.0.0.0"
    SERVER_PORT: int = 8000

    CORS_ALLOWED_ORIGINS: List[str] = [
        "http://localhost:5173", 
        "http://127.0.0.1:5173",
        "http://localhost:8080",      
        "http://127.0.0.1:8080" 
    ]      
    # Database DSN MUST be required
    DATABASE_DSN: str

    MIN_CONNECTION_POOL_SIZE: int = 5
    MAX_CONNECTION_POOL_SIZE: int = 20

    # Supabase Credentials
    SUPABASE_URL: str
    SUPABASE_SERVICE_ROLE_KEY: str

    # --- SECURITY SETTINGS (These were missing) ---
    SECRET_KEY: str
    ALGORITHM: str = "HS256"  # Default for Supabase JWTs
    ENCRYPTION_KEY: str       # Used by app/lib/encryption.py

    # LLM Settings
    OPENAI_API_KEY: Optional[str] = None
    GEMINI_API_KEY: Optional[str] = None
    PERPLEXITY_API_KEY: Optional[str] = None # Added based on your requirements.txt

    LLM_MODEL: str = "gpt-4-turbo"
    LLM_PROVIDER: str = "openai"
    
    # Worker Settings
    MAX_WORKER_THREADS: int = 8

settings = Settings()