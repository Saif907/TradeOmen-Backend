# backend/app/libs/config.py

import os
from pydantic import Field, SecretStr, HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Dict

# --- Define Configuration Tiers and Constants ---

PLAN_TIERS = {
    "FREE": 0,
    "BASIC": 1,
    "PRO": 2,
}

# AI Quota Limits (Enforces Fair Use Policy)
QUOTAS = {
    "AI_CHAT_DAILY_BASIC": 50,    
    "TRADE_LOG_MONTHLY_FREE": 10,
}

# --- Pydantic Base Settings Class ---

class Settings(BaseSettings):
    """
    Centralized configuration class loaded from environment variables.
    Provides structured, immutable access to all application settings.
    (Robustness, Security)
    """
    model_config = SettingsConfigDict(
        env_file='.env',
        env_file_encoding='utf-8',
        extra='ignore'
    )
    
    # --- General Configuration ---
    ENVIRONMENT: str = Field("development", description="Application deployment environment (development/production).")
    TASK_QUEUE_WORKERS: int = Field(4, description="Number of background thread workers for task_queue.")
    
    # --- Supabase & Database Configuration (Security) ---
    SUPABASE_URL: HttpUrl = Field(..., description="Public URL of the Supabase project.")
    SUPABASE_SERVICE_KEY: SecretStr = Field(..., description="Supabase Service Role Key (RLS bypass). CRITICAL.")
    
    # --- Cryptography Key (Privacy) ---
    ENCRYPTION_KEY: SecretStr = Field(..., min_length=44, description="AES-256 Fernet key for content encryption. Must be base64 URL-safe.")
    
    # --- Microservice Configuration ---
    AI_MICROSERVICE_URL: HttpUrl = Field(..., description="Internal URL for the AI Microservice.")
    AI_SERVICE_API_KEY: SecretStr = Field(..., description="Internal API key for AI Microservice communication.")
    LLM_API_KEY: SecretStr = Field(..., description="API key for the external LLM provider (Gemini/OpenAI).")
    
    # --- Billing and Caching Configuration ---
    BILLING_WEBHOOK_SECRET: SecretStr = Field("NO_SECRET", description="Webhook secret for Stripe/Razorpay validation.")
    CACHE_INVALIDATION_URL: HttpUrl = Field("http://localhost:3000/api/revalidate", description="Endpoint for Vercel/Edge cache purging.")
    PREMIUM_FEATURES_ENABLED: bool = Field(False, description="Flag to enable premium features globally.")
    
    # --- App Constants ---
    PLAN_TIERS: Dict[str, int] = PLAN_TIERS
    QUOTAS: Dict[str, int] = QUOTAS
    
    # --- Custom Validation (Ensures Non-breakable secrets) ---
    def model_post_init(self, __context: any) -> None:
        """Post-initialization validation hook."""
        if str(self.ENCRYPTION_KEY) == "a-secure-32-byte-base64-key-here":
            raise ValueError("FATAL: ENCRYPTION_KEY must be replaced with a secure secret in .env.")
            
# Create a singleton instance for global access
settings = Settings()