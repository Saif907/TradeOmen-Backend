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
    
    # URL to redirect the user back to after OAuth success
    # Dev: http://localhost:5173/settings
    FRONTEND_URL: str = "http://localhost:5173" 

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

    # Security
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ENCRYPTION_KEY: str

    # LLM Keys
    OPENAI_API_KEY: Optional[str] = None
    GEMINI_API_KEY: Optional[str] = None
    PERPLEXITY_API_KEY: Optional[str] = None

    LLM_MODEL: str = "gpt-4-turbo"
    LLM_PROVIDER: str = "openai"
    
    MAX_WORKER_THREADS: int = 8

    # --- DHAN OAUTH CREDENTIALS ---
    DHAN_CLIENT_ID: Optional[str] = None
    DHAN_CLIENT_SECRET: Optional[str] = None
    # Must match EXACTLY what you registered with Dhan
    # Example: http://localhost:8000/api/v1/brokers/dhan/callback
    DHAN_REDIRECT_URI: Optional[str] = None 

settings = Settings()