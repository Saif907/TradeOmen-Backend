# backend/app/auth/security.py

import logging
from typing import Dict, Any

from jose import jwt, JWTError, ExpiredSignatureError

from app.core.config import settings

logger = logging.getLogger("tradeomen.auth.security")


class AuthenticationError(Exception):
    """Base authentication error."""


class InvalidTokenError(AuthenticationError):
    """Token is malformed or invalid."""


class ExpiredTokenError(AuthenticationError):
    """Token has expired."""


class InvalidRoleError(AuthenticationError):
    """User role is not allowed."""


class AuthSecurity:
    """
    Cryptographically verifies Supabase-issued JWTs locally.
    No HTTP or FastAPI dependencies.
    """

    @staticmethod
    def verify_token(token: str) -> Dict[str, Any]:
        try:
            payload = jwt.decode(
                token,
                settings.SUPABASE_JWT_SECRET,
                algorithms=[settings.ALGORITHM],
                issuer=settings.SUPABASE_JWT_ISSUER,
                options={
                    "verify_aud": False,   # Supabase API tokens often omit aud
                    "require": ["exp", "sub", "iss"],
                },
            )

            role = payload.get("role")
            if role != "authenticated":
                raise InvalidRoleError(f"Unauthorized role: {role}")

            return payload

        except ExpiredSignatureError:
            logger.info("JWT expired")
            raise ExpiredTokenError("Token expired")

        except JWTError:
            logger.warning("JWT verification failed")
            raise InvalidTokenError("Invalid authentication token")

        except AuthenticationError:
            raise

        except Exception:
            logger.exception("Unexpected authentication error")
            raise AuthenticationError("Authentication failure")
