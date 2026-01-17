# backend/app/core/middleware.py

import time
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp
from fastapi import Request
from app.services.performance_monitor import PerformanceMonitor

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
            
            # Extract User ID if available (set by Auth dependency)
            # This relies on your Auth dependency setting request.state.user_id 
            # or we can extract it if your auth middleware ran already.
            # For strict safety, we often leave user_id None here unless explicitly available.
            user_id = getattr(request.state, "user_id", None)
            
            # Fire and forget (Async)
            await PerformanceMonitor.record_request(
                method=request.method,
                path=request.url.path,
                status_code=status_code,
                duration_ms=duration_ms,
                user_id=user_id
            )