import logging
from typing import List, Dict, Any

from fastapi import Depends, HTTPException, status

from app.auth.dependency import get_current_user
from app.schemas.common_schemas import UserRole

logger = logging.getLogger("tradeomen.auth.permissions")

class RoleChecker:
    """
    Industry-Grade RBAC.
    Usage: Depends(RoleChecker([UserRole.ADMIN]))
    
    Why this is efficient:
    It uses the 'current_user' dependency which is already cached in memory.
    It does NOT query the database again.
    """
    def __init__(self, allowed_roles: List[UserRole]):
        self.allowed_roles = allowed_roles

    def __call__(self, current_user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
        # 1. Get role from the normalized user context
        # We default to USER if the key is missing for safety
        user_role = current_user.get("role", UserRole.USER)

        # 2. Strict Check
        if user_role not in self.allowed_roles:
            logger.warning(
                f"⛔ Access Denied: User {current_user['user_id']} "
                f"with role '{user_role}' tried to access a protected route."
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, 
                detail="Insufficient permissions"
            )
        
        return current_user