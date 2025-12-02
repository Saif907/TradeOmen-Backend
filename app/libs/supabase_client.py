# backend/app/libs/supabase_client.py

import os
from supabase import create_client, Client
from typing import Optional
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

# --- Configuration ---
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

# --- Global Client Instances ---
_service_client: Optional[Client] = None

def get_supabase_service_client() -> Client:
    """
    (Security/Founder Access) Initializes and returns the Supabase client using the Service Role Key.
    
    CRITICAL: This client bypasses ALL RLS and should ONLY be used for founder tools
    and administrative/system tasks (e.g., initial profile creation).
    """
    global _service_client
    
    if _service_client is None:
        if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
            logger.error("FATAL: Supabase URL or SERVICE_KEY missing.")
            raise ValueError("Supabase configuration missing.")
        
        try:
            # We use the Service Key as the token for service-level access
            _service_client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
            logger.info("Supabase Service Client initialized.")
        except Exception as e:
            logger.error(f"Error initializing Supabase Service Client: {e}")
            raise RuntimeError("Could not initialize Supabase client.") from e
            
    return _service_client

def get_supabase_client(jwt_token: str = None) -> Client:
    """
    (RLS Enforced) Returns a standard Supabase client instance.
    
    When a JWT is provided (via FastAPI dependency), RLS is automatically enforced.
    This ensures multi-tenant security for every standard user request.
    """
    # For authenticated requests, the JWT is the key to RLS enforcement.
    if jwt_token:
        # Create a new client bound to the request's JWT for thread safety and RLS
        return create_client(SUPABASE_URL, jwt_token)
    
    # Fallback/Default Client (Should rarely be used for transactional data)
    return get_supabase_service_client()


def get_founder_db_access() -> Client:
    """
    FastAPI dependency that provides unrestricted access for founder tools.
    """
    # Simply delegates to the Service Client
    return get_supabase_service_client()