# backend/app/auth/jwt_handler.py

import os
import httpx # Use async httpx for non-blocking I/O
from typing import Optional
from fastapi import HTTPException, status
from jose import jwt, jwk
from jose.exceptions import JWTError
from loguru import logger
from uuid import UUID

from app.libs.config import settings
from app.libs.data_models import UserToken

# --- Global Async Client Initialization (Efficiency) ---

# We rely on FastAPI's lifespan events in main.py to manage the client lifecycle.
ASYNC_HTTP_CLIENT: Optional[httpx.AsyncClient] = None
JWKS_CACHE = None
LAST_FETCHED = 0
JWKS_CACHE_TTL = 3600 # 1 hour cache time-to-live

def set_async_client(client: httpx.AsyncClient):
    """Sets the global httpx.AsyncClient instance."""
    global ASYNC_HTTP_CLIENT
    ASYNC_HTTP_CLIENT = client

# --- Custom Exception (Non-Breakable) ---

class JWTAuthError(HTTPException):
    """Custom exception for all JWT authentication failures."""
    def __init__(self, detail: str, status_code: int = status.HTTP_401_UNAUTHORIZED):
        super().__init__(status_code=status_code, detail=detail)

# --- Core Logic (Secure & Reusable) ---

async def _fetch_jwks():
    """
    (Super Fast/Async) Fetches and caches the JSON Web Key Set (JWKS) from Supabase.
    Uses async httpx to ensure non-blocking I/O.
    """
    global JWKS_CACHE, LAST_FETCHED
    
    current_time = os.time()
    
    if JWKS_CACHE and (current_time - LAST_FETCHED) < JWKS_CACHE_TTL:
        logger.debug("Using cached JWKS.")
        return JWKS_CACHE

    if not ASYNC_HTTP_CLIENT:
         raise RuntimeError("Internal HTTP client not initialized.") 

    # Fetching keys from Supabase auth endpoint
    jwks_url = f"{settings.SUPABASE_URL}/auth/v1/keys"
    
    try:
        # Use ASYNC_HTTP_CLIENT for non-blocking I/O
        response = await ASYNC_HTTP_CLIENT.get(jwks_url, timeout=5)
        response.raise_for_status()
        
        jwks_data = response.json()
        
        JWKS_CACHE = jwks_data
        LAST_FETCHED = current_time
        logger.success("Successfully fetched and cached new JWKS.")
        return jwks_data
        
    except httpx.RequestError as e:
        logger.error(f"Failed to fetch JWKS from Supabase (HTTP Error): {e}")
        raise JWTAuthError(detail="Authentication service endpoint unavailable.")
    except Exception as e:
         logger.error(f"Failed to fetch JWKS (General Error): {e}")
         raise JWTAuthError(detail="Internal error fetching authentication keys.")


async def decode_and_validate_jwt(token: str) -> UserToken:
    """
    (Security) Decodes and validates the Supabase JWT and extracts the user's UUID.
    """
    try:
        jwks_data = await _fetch_jwks() 
        
        unverified_header = jwt.get_unverified_header(token)
        kid = unverified_header.get('kid')
        
        if not kid:
            raise JWTAuthError(detail="Token header missing 'kid'.")

        key = next((k for k in jwks_data['keys'] if k['kid'] == kid), None)
        if not key:
            raise JWTAuthError(detail=f"No matching key found for kid '{kid}'.")

        public_key = jwk.construct(key).to_pem().decode()

        payload = jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            audience="authenticated"
        )
        
        return UserToken(user_id=UUID(payload['sub']))
        
    except JWTAuthError:
        raise
    except JWTError as e:
        logger.warning(f"JWT Validation Failed ({e.__class__.__name__}): {e}")
        raise JWTAuthError(detail=f"Token validation failed: {e.__class__.__name__}")
    except Exception as e:
        logger.error(f"Unexpected error during JWT handling: {e}")
        raise JWTAuthError(detail="Internal error during token verification.")