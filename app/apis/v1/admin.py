# backend/app/apis/v1/admin.py

from fastapi import APIRouter, Depends, HTTPException, status
from typing import Dict, Any, List
from app.auth.dependency import get_current_user
from app.core.database import db

router = APIRouter()

# ---------------------------------------------------------
# Security: Ensure only Admins can access this
# ---------------------------------------------------------
def require_admin(current_user: Dict[str, Any] = Depends(get_current_user)):
    # Assuming your JWT has a "role" claim, or you check specific email
    # user_role = current_user.get("role", "user")
    # if user_role != "admin":
    
    # Simple email check for now (Update with your email)
    if current_user.get("email") not in ["saifshaikh@tradeomen.com", "saif81868@gmail.com"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, 
            detail="Admin access required"
        )
    return current_user

# ---------------------------------------------------------
# Endpoint 1: The "Big Numbers" Cards
# ---------------------------------------------------------
@router.get("/stats/summary")
async def get_admin_summary(admin: Dict[str, Any] = Depends(require_admin)):
    """
    Returns data for the top cards: Total Users, Active Today, Error Rate.
    """
    
    # 1. Total Users
    total_users = await db.fetch_val("SELECT COUNT(*) FROM user_profiles")
    
    # 2. Active Today (Unique users in user_events in last 24h)
    active_today = await db.fetch_val("""
        SELECT COUNT(DISTINCT user_id) 
        FROM user_events 
        WHERE created_at > NOW() - INTERVAL '24 hours'
    """)
    
    # 3. System Health (From our Performance Monitor)
    # Get the latest report
    latest_report = await db.fetch_one("""
        SELECT details 
        FROM user_events 
        WHERE event_type = 'PERFORMANCE_REPORT' 
        ORDER BY created_at DESC 
        LIMIT 1
    """)
    
    health_data = latest_report["details"] if latest_report else {}
    error_count = 0
    
    # Sum up errors from the latest report
    if "requests" in health_data:
        for req_stats in health_data["requests"].values():
            error_count += req_stats.get("errors", 0)

    return {
        "total_users": total_users,
        "active_24h": active_today or 0,
        "current_errors": error_count,
        "db_pool_status": health_data.get("db_pool", "Unknown")
    }

# ---------------------------------------------------------
# Endpoint 2: The "Performance Graph"
# ---------------------------------------------------------
@router.get("/stats/performance")
async def get_performance_graph(admin: Dict[str, Any] = Depends(require_admin)):
    """
    Returns data for the main chart (Latency & Traffic over time).
    """
    # Get last 60 reports (1 hour of data)
    rows = await db.fetch_all("""
        SELECT 
            created_at,
            (details->'db'->>'queries')::int as db_queries,
            (details->'active_users_count')::int as active_users
        FROM user_events
        WHERE event_type = 'PERFORMANCE_REPORT'
        ORDER BY created_at DESC
        LIMIT 60
    """)
    
    # Reverse to show oldest -> newest on graph
    return list(reversed([dict(row) for row in rows]))