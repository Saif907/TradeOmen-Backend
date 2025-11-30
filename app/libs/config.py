from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional

class Settings(BaseSettings):
    # ----------------------------------------------------------------------
    # Core Application & Security
    # ----------------------------------------------------------------------
    # Used for internal JWT signing/hashing (e.g., password reset tokens)
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    
    # Data Privacy and Encryption
    # 32-byte Base64 key for AES-256 encryption/decryption of sensitive notes
    DATA_ENCRYPTION_KEY: str
    
    # ----------------------------------------------------------------------
    # Supabase Configuration
    # ----------------------------------------------------------------------
    SUPABASE_URL: str
    SUPABASE_KEY: str
    # Service key is sensitive, use only when necessary on the backend
    SUPABASE_SERVICE_KEY: str 

    # ----------------------------------------------------------------------
    # AI Microservice Communication
    # ----------------------------------------------------------------------
    # Base URL for the AI Microservice (e.g., http://ai-service-host:8001)
    AI_MICROSERVICE_URL: str
    # Shared secret key for the Main Backend to authenticate with the AI service
    AI_SERVICE_SECRET_KEY: str 
    
    # ----------------------------------------------------------------------
    # Pydantic Settings Configuration
    # ----------------------------------------------------------------------
    model_config = SettingsConfigDict(
        env_file=".env", 
        case_sensitive=True,
        # 'ignore' ensures validation does not fail if non-defined variables are present in .env
        extra="ignore" 
    )

# Instantiate the settings object to be imported across the application
settings = Settings()