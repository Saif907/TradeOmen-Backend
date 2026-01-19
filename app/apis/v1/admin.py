from fastapi import APIRouter, Depends, HTTPException, Body, Request # ✅ Added Request
from typing import Dict, Any, List, Optional
from pydantic import BaseModel

from app.core.database import db
from app.auth.permissions import RoleChecker
from app.schemas.common_schemas import UserRole
from app.core.config import settings

# ✅ FIX: Import cache invalidation function from dependency.py
# This is critical for making updates appear instantly.
from app.auth.dependency import invalidate_user_cache
from app.core.limiter import limiter # ✅ Rate Limiter Import

router = APIRouter()

# ---------------------------------------------------------
# Security Policy
# ---------------------------------------------------------
allow_admin = RoleChecker([UserRole.ADMIN, UserRole.SUPER_ADMIN])

# ---------------------------------------------------------
# Data Schemas
# ---------------------------------------------------------
class PlanUpdate(BaseModel):
    plan_tier: str

class BanUpdate(BaseModel):
    is_banned: bool
    reason: Optional[str] = None

class ProfileUpdate(BaseModel):
    full_name: Optional[str] = None
    role: Optional[str] = None

# ---------------------------------------------------------
# Endpoint 1: Admin Stats
# ---------------------------------------------------------
@router.get("/stats/summary", dependencies=[Depends(allow_admin)])
@limiter.limit("60/minute") # ✅ Admin Dashboard Load
async def get_admin_summary(request: Request):
    try:
        total_users = await db.fetch_val("SELECT COUNT(*) FROM user_profiles") or 0
        active_today = await db.fetch_val("""
            SELECT COUNT(*) FROM user_profiles 
            WHERE last_active_at > NOW() - INTERVAL '24 hours'
        """) or 0
        return {
            "total_users": total_users,
            "active_24h": active_today,
            "current_errors": 0,
            "db_pool_status": "Healthy"
        }
    except Exception as e:
        print(f"[Admin Stats Error] {e}")
        return {"total_users": 0, "active_24h": 0, "current_errors": 0, "db_pool_status": "Error"}

# ---------------------------------------------------------
# Endpoint 2: Global Configuration (Plans)
# ---------------------------------------------------------
@router.get("/config/plans", dependencies=[Depends(allow_admin)])
@limiter.limit("60/minute") # ✅ Admin Config Load
async def get_plans_config(request: Request):
    """
    Returns the centralized plan definitions from config.py.
    This allows the Admin UI to show limits dynamically.
    """
    return {
        "plans": settings.PLAN_DEFINITIONS,
        "default_plan": settings.DEFAULT_PLAN,
        "order": settings.PLAN_ORDER
    }

# ---------------------------------------------------------
# Endpoint 3: User Management (Write Operations)
# ---------------------------------------------------------
@router.get("/users/{user_id}", dependencies=[Depends(allow_admin)])
@limiter.limit("60/minute") # ✅ Fast Browsing
async def get_user_details(request: Request, user_id: str):
    query = "SELECT * FROM public.user_profiles WHERE id = $1"
    user = await db.fetch_one(query, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return dict(user)

@router.post("/users/{user_id}/ban", dependencies=[Depends(allow_admin)])
@limiter.limit("30/minute") # ✅ Critical Action Protection
async def ban_user(request: Request, user_id: str, body: BanUpdate):
    # Toggle 'banned' status in preferences
    query = """
        UPDATE public.user_profiles
        SET preferences = jsonb_set(COALESCE(preferences, '{}'), '{account_status}', $2)
        WHERE id = $1
        RETURNING id
    """
    status_val = '"banned"' if body.is_banned else '"active"'
    updated = await db.fetch_one(query, user_id, status_val)
    
    if not updated:
        raise HTTPException(status_code=404, detail="User not found")
    
    # ✅ FIX: Invalidate cache immediately so ban takes effect instantly
    invalidate_user_cache(user_id)
    
    return {"message": f"User {'banned' if body.is_banned else 'unbanned'}"}

@router.put("/users/{user_id}/plan", dependencies=[Depends(allow_admin)])
@limiter.limit("30/minute") # ✅ Critical Action Protection
async def update_user_plan(request: Request, user_id: str, body: PlanUpdate):
    # Validate plan against config
    if body.plan_tier.upper() not in settings.PLAN_DEFINITIONS:
        raise HTTPException(status_code=400, detail=f"Invalid plan. Options: {settings.PLAN_ORDER}")

    # Update Query
    # Note: We update BOTH plan_tier and active_plan_id to be safe and consistent
    query = """
        UPDATE public.user_profiles
        SET plan_tier = $2, active_plan_id = $2, updated_at = NOW()
        WHERE id = $1
        RETURNING id, plan_tier
    """
    updated = await db.fetch_one(query, user_id, body.plan_tier.upper())
    
    if not updated:
        raise HTTPException(status_code=404, detail="User not found")
    
    # ✅ FIX: Invalidate cache immediately so plan change takes effect instantly
    # This solves the "second change not working" issue
    invalidate_user_cache(user_id)

    return {"message": "Plan updated", "new_plan": updated['plan_tier']}

@router.put("/users/{user_id}/profile", dependencies=[Depends(allow_admin)])
@limiter.limit("30/minute") # ✅ Critical Action Protection
async def update_user_profile(request: Request, user_id: str, body: ProfileUpdate):
    fields = []
    values = [user_id]
    idx = 2
    
    if body.full_name is not None:
        fields.append(f"full_name = ${idx}")
        values.append(body.full_name)
        idx += 1
    if body.role is not None:
        fields.append(f"role = ${idx}")
        values.append(body.role)
        idx += 1
        
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")
        
    query = f"UPDATE public.user_profiles SET {', '.join(fields)} WHERE id = $1 RETURNING id"
    updated = await db.fetch_one(query, *values)
    
    if not updated:
        raise HTTPException(status_code=404, detail="User not found")
    
    # ✅ FIX: Invalidate cache immediately so role/name changes take effect instantly
    invalidate_user_cache(user_id)

    return {"message": "Profile updated"}