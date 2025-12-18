# backend/app/apis/v1/metrics.py
from fastapi import APIRouter, Depends, BackgroundTasks, Request
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

@router.get("/insights", response_model=Dict[str, Any])
async def get_my_insights(
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Returns a calculated report of the user's interaction with the platform.
    Includes: Health Score, Persona, and Usage Stats.
    """
    user_id = current_user["sub"]
    
    # 1. Get Behavioral Insights
    insights = await MetricsEngine.get_user_insights(user_id)
    
    # 2. Get Quota Usage (from previous service)
    quota_report = await QuotaManager.get_user_usage_report(user_id)
    
    # Merge them
    return {
        "insights": insights,
        "quota": quota_report
    }

@router.post("/telemetry", status_code=201)
async def report_client_telemetry(
    payload: TelemetryRequest,
    background_tasks: BackgroundTasks,
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Endpoint for Frontend to log errors or events.
    Example: Frontend catches a 'Network Error' and sends it here.
    """
    user_id = current_user["sub"]
    
    # Sanitize inputs
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