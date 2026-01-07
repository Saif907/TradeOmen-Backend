# backend/app/core/config.py

import os
import sys
from typing import List, Optional, Dict, Any, Union, Literal
from copy import deepcopy
from pydantic import AnyHttpUrl, field_validator, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict
from dotenv import load_dotenv

load_dotenv()

AppEnvironment = Literal["development", "test", "staging", "production"]

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True
    )

    # --- 1. Environment & Safety ---
    APP_NAME: str = "TradeOmen AI Backend"
    APP_VERSION: str = "1.2.0" # Bumped for Quota Fixes
    APP_ENV: AppEnvironment = "development"
    LOG_LEVEL: str = "INFO"

    @computed_field
    @property
    def IS_DEV(self) -> bool: return self.APP_ENV == "development"

    @computed_field
    @property
    def IS_TEST(self) -> bool: return self.APP_ENV == "test"

    @computed_field
    @property
    def IS_STAGING(self) -> bool: return self.APP_ENV == "staging"

    @computed_field
    @property
    def IS_PROD(self) -> bool: return self.APP_ENV == "production"

    def model_post_init(self, __context: Any) -> None:
        if self.IS_PROD:
            if not self.OPENAI_API_KEY and not self.GEMINI_API_KEY:
                 sys.stderr.write("🚨 FATAL: No AI API key configured for production! System cannot start.\n")
                 sys.exit(1)
            if not self.ENCRYPTION_KEY or self.ENCRYPTION_KEY == "change-me":
                 sys.stderr.write("🚨 FATAL: Weak or missing ENCRYPTION_KEY in production.\n")
                 sys.exit(1)

    # --- Server ---
    SERVER_HOST: str = "0.0.0.0"
    SERVER_PORT: int = 8000
    
    # --- CORS ---
    BACKEND_CORS_ORIGINS: List[AnyHttpUrl] = []

    @field_validator("BACKEND_CORS_ORIGINS", mode="before")
    @classmethod
    def assemble_cors_origins(cls, v: Union[str, List[str]]) -> List[str]:
        if isinstance(v, str) and not v.startswith("["):
            return [i.strip() for i in v.split(",")]
        elif isinstance(v, (list, str)):
            return v
        return []

    # --- Database ---
    DATABASE_DSN: str
    MIN_CONNECTION_POOL_SIZE: int = 5
    MAX_CONNECTION_POOL_SIZE: int = 20

    # --- Supabase ---
    SUPABASE_URL: str
    SUPABASE_SERVICE_ROLE_KEY: str
    SUPABASE_ANON_KEY: str

    # --- Storage ---
    SCREENSHOT_BUCKET: str = "trade-screenshots"
    MAX_UPLOAD_SIZE_BYTES: int = 5 * 1024 * 1024
    ALLOWED_IMAGE_TYPES: List[str] = ["image/png", "image/jpeg", "image/jpg", "image/webp"]

    # --- Security ---
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ENCRYPTION_KEY: str

    # --- AI ---
    OPENAI_API_KEY: Optional[str] = None
    GEMINI_API_KEY: Optional[str] = None
    PERPLEXITY_API_KEY: Optional[str] = None
    LLM_PROVIDER: str = "gemini"
    LLM_MODEL: str = "gemini-1.5-flash"
    
    # --- RAG ---
    EMBEDDING_PROVIDER: str = "openai"
    EMBEDDING_MODEL: str = "text-embedding-3-small"
    VECTOR_STORE_TABLE: str = "embeddings"
    MAX_WORKER_THREADS: int = 8

    # --- Plan Definitions ---
    DEFAULT_PLAN: str = "FREE"
    PLAN_ORDER: List[str] = ["FREE", "PRO", "PREMIUM"]

    PLAN_DEFINITIONS: Dict[str, Dict[str, Any]] = {
        "FREE": {
            "display_name": "Free",
            # Usage Limits (None = Unlimited)
            "max_trades_per_month": 30, # ✅ Monthly Limit
            "max_strategies": 1,
            "daily_chat_msgs": 5,
            "monthly_ai_tokens_limit": 50_000,
            "monthly_csv_imports": 0,
            "max_broker_accounts": 0,
            
            # Feature Flags
            "allow_tags": False,
            "allow_screenshots": False,
            "allow_web_search": False,
            "allow_deep_research": False,
            "allow_export_csv": False, # ✅ Consistent Naming
            "allow_broker_sync": False,
        },
        "PRO": {
            "display_name": "Pro",
            "max_trades_per_month": None, # Unlimited
            "max_strategies": 10,
            "daily_chat_msgs": 50,
            "monthly_ai_tokens_limit": 2_000_000,
            "monthly_csv_imports": 10,
            "max_broker_accounts": 1,
            
            "allow_tags": True,
            "allow_screenshots": True,
            "allow_web_search": False,
            "allow_deep_research": False,
            "allow_export_csv": True, # ✅ Consistent Naming
            "allow_broker_sync": True,
        },
        "PREMIUM": {
            "display_name": "Premium",
            "max_trades_per_month": None,
            "max_strategies": None,
            "daily_chat_msgs": 200,
            "monthly_ai_tokens_limit": 10_000_000,
            "monthly_csv_imports": None,
            "max_broker_accounts": 10,
            
            "allow_tags": True,
            "allow_screenshots": True,
            "allow_web_search": True,
            "allow_deep_research": True,
            "allow_export_csv": True,
            "allow_broker_sync": True,
        }
    }

    def get_plan_limits(self, plan_tier: str) -> Dict[str, Any]:
        plan_key = (plan_tier or self.DEFAULT_PLAN).upper()
        if plan_key not in self.PLAN_DEFINITIONS:
            plan_key = self.DEFAULT_PLAN
            
        limits = deepcopy(self.PLAN_DEFINITIONS[plan_key])

        if self.IS_DEV:
            for key, val in limits.items():
                if isinstance(val, (int, float)) and val is not None:
                     if key in ["daily_chat_msgs", "monthly_ai_tokens_limit", "monthly_csv_imports"]:
                        limits[key] = val * 1000
        
        return limits

    # --- Broker Integrations ---
    DHAN_CLIENT_ID: Optional[str] = None
    DHAN_CLIENT_SECRET: Optional[str] = None
    DHAN_REDIRECT_URI: str = "http://localhost:8000/api/v1/brokers/dhan/callback"

settings = Settings()