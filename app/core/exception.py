# app/core/exception.py

import logging
from typing import Any, Dict

from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("tradeomen.exceptions")


# ------------------------------------------------------------------------------
# Helper: Standard Error Response
# ------------------------------------------------------------------------------

def error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    details: Any = None,
) -> JSONResponse:
    payload: Dict[str, Any] = {
        "error": {
            "code": code,
            "message": message,
        }
    }

    if details is not None:
        payload["error"]["details"] = details

    return JSONResponse(status_code=status_code, content=payload)


# ------------------------------------------------------------------------------
# Global Exception Handler (500)
# ------------------------------------------------------------------------------

async def global_exception_handler(request: Request, exc: Exception):
    """
    Catch-all handler for unexpected server errors.
    """

    logger.exception(
        "Unhandled exception",
        extra={
            "method": request.method,
            "path": request.url.path,
            "query": str(request.url.query),
        },
    )

    return error_response(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        code="INTERNAL_SERVER_ERROR",
        message="Something went wrong. Please try again later.",
    )


# ------------------------------------------------------------------------------
# HTTP Exception Handler (4xx / 5xx)
# ------------------------------------------------------------------------------

async def http_exception_handler(
    request: Request,
    exc: StarletteHTTPException,
):
    """
    Handles HTTP exceptions raised explicitly by the application.
    """

    log_level = logging.WARNING if exc.status_code < 500 else logging.ERROR

    logger.log(
        log_level,
        "HTTP exception",
        extra={
            "status_code": exc.status_code,
            "method": request.method,
            "path": request.url.path,
            "detail": exc.detail,
        },
    )

    return error_response(
        status_code=exc.status_code,
        code="HTTP_ERROR",
        message=str(exc.detail),
    )


# ------------------------------------------------------------------------------
# Validation Error Handler (422)
# ------------------------------------------------------------------------------

async def validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
):
    """
    Handles request validation errors.
    """

    formatted_errors = []

    for error in exc.errors():
        location = ".".join(str(x) for x in error.get("loc", []))
        formatted_errors.append(
            {
                "field": location or "body",
                "message": error.get("msg"),
                "type": error.get("type"),
            }
        )

    logger.info(
        "Validation error",
        extra={
            "method": request.method,
            "path": request.url.path,
            "error_count": len(formatted_errors),
        },
    )

    return error_response(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        code="VALIDATION_ERROR",
        message="Invalid request data",
        details=formatted_errors,
    )
