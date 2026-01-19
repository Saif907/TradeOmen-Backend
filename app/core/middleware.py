# backend/app/core/middleware.py

import time
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp
from fastapi import Request
from app.services.performance_monitor import PerformanceMonitor
from app.services.analytics import Analytics # ✅ Added Analytics Import

class APIMonitorMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start_time = time.time()
        
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        except Exception:
            status_code = 500
            raise
        finally:
            # Calculate duration
            duration_ms = (time.time() - start_time) * 1000
            
            # Extract User ID if available
            # We check both user_id and the full user object if set by dependency
            user_id = getattr(request.state, "user_id", None)
            if not user_id and hasattr(request.state, "user"):
                user_id = request.state.user.get("id") or request.state.user.get("user_id")
            
            # 1. Internal Metrics (Postgres/Performance Monitor)
            await PerformanceMonitor.record_request(
                method=request.method,
                path=request.url.path,
                status_code=status_code,
                duration_ms=duration_ms,
                user_id=user_id
            )

            # 2. External Analytics (PostHog)
            # Only capture if we have a user_id to avoid polluting with anonymous noise
            # and only for actual API routes (ignoring docs/health)
            if user_id and not request.url.path.startswith(("/docs", "/redoc", "/openapi.json")):
                Analytics.capture(
                    user_id=str(user_id),
                    event_name="api_request_processed",
                    properties={
                        "method": request.method,
                        "path": request.url.path,
                        "status_code": status_code,
                        "duration_ms": round(duration_ms, 2),
                        "is_error": status_code >= 400
                    }
                )