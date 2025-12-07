# backend/app/auth/security.py
from jose import jwt, JWTError
from fastapi import HTTPException, status
from app.core.config import settings
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)

class AuthSecurity:
    """
    Handles cryptographic verification of Supabase JWTs locally.
    This avoids an expensive HTTP round-trip to Supabase Auth for every API request.
    """
    
    @staticmethod
    def verify_token(token: str) -> Dict[str, Any]:
        """
        Decodes and validates a JWT token.
        
        Args:
            token (str): The raw JWT string from the Authorization header.
            
        Returns:
            Dict[str, Any]: The decoded user payload (sub, email, role, metadata).
            
        Raises:
            HTTPException: If token is expired, invalid, or signature verification fails.
        """
        try:
            # Decode the token using the Supabase JWT Secret
            # verify_exp=True ensures we reject expired tokens automatically
            payload = jwt.decode(
                token, 
                settings.SECRET_KEY, 
                algorithms=[settings.ALGORITHM],
                options={"verify_aud": False} # Supabase JWTs often don't have a standard 'aud' claim for the API
            )
            
            # Additional check: Ensure it's an authenticated user, not an anonymous one (if you block anon)
            if payload.get("role") != "authenticated":
                logger.warning(f"Rejected token with role: {payload.get('role')}")
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Invalid user role"
                )
                
            return payload

        except jwt.ExpiredSignatureError:
            logger.info("Token expired")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token expired",
                headers={"WWW-Authenticate": "Bearer"},
            )
        except JWTError as e:
            logger.error(f"JWT Verification Failed: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Could not validate credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )
        except Exception as e:
            logger.error(f"Unexpected Auth Error: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Authentication service error"
            )