# backend/app/core/config.py

import os
import sys
from typing import (
    List,
    Optional,
    Dict,
    Any,
    Union,
    Literal,
    ClassVar,
    Set,
)
from copy import deepcopy

from pydantic import AnyHttpUrl, field_validator, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict
from dotenv import load_dotenv


# --------------------------------------------------
# Load .env ONLY for non-production environments
# --------------------------------------------------
if os.getenv("ENVIRONMENT") != "production":
    load_dotenv()


AppEnvironment = Literal["development", "test", "staging", "production"]


class Settings(BaseSettings):
    """
    Central configuration for TradeOmen backend.
    Strict, explicit, and fail-fast (Pydantic v2 compliant).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    # --------------------------------------------------
    # 1. Environment & Project Info
    # --------------------------------------------------
    # Renamed APP_NAME -> PROJECT_NAME to match main.py
    PROJECT_NAME: str = "TradeOmen AI Backend" 
    APP_VERSION: str = "1.2.0"
    
    # Renamed APP_ENV -> ENVIRONMENT to match main.py
    ENVIRONMENT: AppEnvironment = "development" 
    LOG_LEVEL: str = "INFO"
    
    # Added API prefix to match main.py expectations
    API_V1_STR: str = "/api/v1"

    # ---- CONSTANTS (NOT ENV FIELDS) ----
    VALID_LOG_LEVELS: ClassVar[Set[str]] = {
        "DEBUG",
        "INFO",
        "WARNING",
        "ERROR",
        "CRITICAL",
    }

    @computed_field
    @property
    def IS_DEV(self) -> bool:
        return self.ENVIRONMENT == "development"

    @computed_field
    @property
    def IS_TEST(self) -> bool:
        return self.ENVIRONMENT == "test"

    @computed_field
    @property
    def IS_STAGING(self) -> bool:
        return self.ENVIRONMENT == "staging"

    @computed_field
    @property
    def IS_PROD(self) -> bool:
        return self.ENVIRONMENT == "production"

    # --------------------------------------------------
    # 2. Server
    # --------------------------------------------------
    SERVER_HOST: str = "0.0.0.0"
    SERVER_PORT: int = 8000

    # --------------------------------------------------
    # 3. CORS
    # --------------------------------------------------
    # Renamed CORS_ALLOWED_ORIGINS -> BACKEND_CORS_ORIGINS to match main.py
    BACKEND_CORS_ORIGINS: List[AnyHttpUrl] = []

    @field_validator("BACKEND_CORS_ORIGINS", mode="before")
    @classmethod
    def assemble_cors_origins(cls, v: Union[str, List[str]]) -> List[str]:
        if isinstance(v, str) and not v.startswith("["):
            return [i.strip() for i in v.split(",") if i.strip()]
        if isinstance(v, list):
            return v
        return []

    # --------------------------------------------------
    # 4. Database
    # --------------------------------------------------
    DATABASE_DSN: Optional[str] = None
    MIN_CONNECTION_POOL_SIZE: int = 5
    MAX_CONNECTION_POOL_SIZE: int = 20

    # --------------------------------------------------
    # 5. Supabase
    # --------------------------------------------------
    SUPABASE_URL: Optional[str] = None
    SUPABASE_SERVICE_ROLE_KEY: Optional[str] = None
    SUPABASE_ANON_KEY: Optional[str] = None

    # --------------------------------------------------
    # 6. Storage
    # --------------------------------------------------
    SCREENSHOT_BUCKET: str = "trade-screenshots"
    MAX_UPLOAD_SIZE_BYTES: int = 5 * 1024 * 1024
    ALLOWED_IMAGE_TYPES: List[str] = [
        "image/png",
        "image/jpeg",
        "image/jpg",
        "image/webp",
    ]

    # --------------------------------------------------
    # 7. Security
    # --------------------------------------------------
    SECRET_KEY: Optional[str] = None
    ALGORITHM: str = "HS256"
    ENCRYPTION_KEY: Optional[str] = None

    # --------------------------------------------------
    # 8. AI / LLM
    # --------------------------------------------------
    OPENAI_API_KEY: Optional[str] = None
    GEMINI_API_KEY: Optional[str] = None
    PERPLEXITY_API_KEY: Optional[str] = None

    LLM_PROVIDER: Literal["openai", "gemini", "perplexity"] = "gemini"
    LLM_MODEL: str = "gemini-1.5-flash"

    SANITIZE_PII: bool = True

    # --------------------------------------------------
    # 9. RAG / Workers
    # --------------------------------------------------
    EMBEDDING_PROVIDER: Literal["openai", "gemini"] = "openai"
    EMBEDDING_MODEL: str = "text-embedding-3-small"
    VECTOR_STORE_TABLE: str = "embeddings"
    MAX_WORKER_THREADS: int = 8

    # --------------------------------------------------
    # 10. Plans & Monetization (CONSTANTS)
    # --------------------------------------------------
    DEFAULT_PLAN: str = "FREE"

    PLAN_ORDER: ClassVar[List[str]] = ["FREE", "PRO", "PREMIUM"]

    PLAN_DEFINITIONS: ClassVar[Dict[str, Dict[str, Any]]] = {
        "FREE": {
            "display_name": "Free",
            "max_trades_per_month": 30,
            "max_strategies": 1,
            "daily_chat_msgs": 5,
            "monthly_ai_tokens_limit": 50_000,
            "monthly_csv_imports": 0,
            "max_broker_accounts": 0,
            "allow_tags": False,
            "allow_screenshots": False,
            "allow_web_search": False,
            "allow_deep_research": False,
            "allow_export_csv": False,
            "allow_broker_sync": False,
        },
        "PRO": {
            "display_name": "Pro",
            "max_trades_per_month": None,
            "max_strategies": 10,
            "daily_chat_msgs": 50,
            "monthly_ai_tokens_limit": 2_000_000,
            "monthly_csv_imports": 10,
            "max_broker_accounts": 1,
            "allow_tags": True,
            "allow_screenshots": True,
            "allow_web_search": False,
            "allow_deep_research": False,
            "allow_export_csv": True,
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
        },
    }

    def get_plan_limits(self, plan_tier: str) -> Dict[str, Any]:
        plan_key = (plan_tier or self.DEFAULT_PLAN).upper()
        if plan_key not in self.PLAN_DEFINITIONS:
            plan_key = self.DEFAULT_PLAN

        limits = deepcopy(self.PLAN_DEFINITIONS[plan_key])

        # Inflate limits in DEV only
        if self.IS_DEV:
            for key, val in limits.items():
                if isinstance(val, int) and key in {
                    "daily_chat_msgs",
                    "monthly_ai_tokens_limit",
                    "monthly_csv_imports",
                }:
                    limits[key] = val * 1000

        return limits

    # --------------------------------------------------
    # 11. Broker Integrations
    # --------------------------------------------------
    DHAN_CLIENT_ID: Optional[str] = None
    DHAN_CLIENT_SECRET: Optional[str] = None
    DHAN_REDIRECT_URI: str = "http://localhost:8000/api/v1/brokers/dhan/callback"

    # --------------------------------------------------
    # 12. Final Hard Validation (Fail Fast)
    # --------------------------------------------------
    def model_post_init(self, __context: Any) -> None:
        if self.LOG_LEVEL not in self.VALID_LOG_LEVELS:
            sys.exit(f"❌ Invalid LOG_LEVEL: {self.LOG_LEVEL}")

        if self.IS_PROD:
            if not self.SECRET_KEY:
                sys.exit("❌ FATAL: SECRET_KEY missing in production")

            if not self.DATABASE_DSN:
                sys.exit("❌ FATAL: DATABASE_DSN missing in production")

            if self.LLM_PROVIDER == "openai" and not self.OPENAI_API_KEY:
                sys.exit("❌ FATAL: OPENAI_API_KEY required for OpenAI provider")

            if self.LLM_PROVIDER == "gemini" and not self.GEMINI_API_KEY:
                sys.exit("❌ FATAL: GEMINI_API_KEY required for Gemini provider")

            if self.LLM_PROVIDER == "perplexity" and not self.PERPLEXITY_API_KEY:
                sys.exit("❌ FATAL: PERPLEXITY_API_KEY required for Perplexity provider")


# Singleton
settings = Settings()