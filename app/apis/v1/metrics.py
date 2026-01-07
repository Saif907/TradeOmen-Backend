# backend/app/apis/v1/metrics.py
from fastapi import APIRouter, Depends, BackgroundTasks, Query
from typing import Dict, Any, Optional
from pydantic import BaseModel

from app.auth.dependency import get_current_user
from app.services.metrics_engine import MetricsEngine
from app.services.quota_manager import QuotaManager

router = APIRouter()

class TelemetryRequest(BaseModel):
    event_type: str
    category: str = "INFO"
    details: Dict[str, Any] = {}
    path: Optional[str] = None

# ---------------------------------------------------------
# 1. USER ANALYTICS (Behavior & Health)
# ---------------------------------------------------------

@router.get("/insights", response_model=Dict[str, Any])
async def get_my_insights(
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Returns a calculated report of the user's interaction with the platform.
    Includes: Health Score, Persona, and Plan Limits.
    """
    user_id = current_user["sub"]
    
    # Parallelize these if latency becomes an issue later
    insights = await MetricsEngine.get_user_insights(user_id)
    quota_report = await QuotaManager.get_user_usage_report(user_id)
    
    return {
        "insights": insights,
        "quota": quota_report
    }

# ---------------------------------------------------------
# 2. FINANCIAL ANALYTICS (AI Cost) - NEW
# ---------------------------------------------------------

@router.get("/ai-usage", response_model=Dict[str, Any])
async def get_ai_usage_report(
    days: int = Query(30, ge=1, le=90, description="Lookback period in days"),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Returns detailed AI spend analysis.
    Useful for 'Settings > Billing' or Admin Dashboards.
    """
    user_id = current_user["sub"]
    return await MetricsEngine.get_ai_spend_analytics(user_id, days)

# ---------------------------------------------------------
# 3. CLIENT TELEMETRY (Logs from Frontend)
# ---------------------------------------------------------

@router.post("/telemetry", status_code=201)
async def report_client_telemetry(
    payload: TelemetryRequest,
    background_tasks: BackgroundTasks,
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Log errors/events from the Frontend (e.g., 'Chart Failed to Load').
    """
    user_id = current_user["sub"]
    
    # Sanitize category
    category = payload.category.upper()
    if category not in ["INFO", "WARNING", "ERROR", "CRITICAL"]:
        category = "INFO"

    background_tasks.add_task(
        MetricsEngine.log_telemetry,
        user_id=user_id,
        event_type=payload.event_type,
        category=category,
        details=payload.details,
        path=payload.path
    )
    
    return {"status": "logged"}