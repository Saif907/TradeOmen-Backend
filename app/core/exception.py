from fastapi import Request, status
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
import logging

logger = logging.getLogger(__name__)

async def global_exception_handler(request: Request, exc: Exception):
    """
    Catch-all handler for unhandled exceptions (Internal Server Errors).
    Logs the error and returns a generic 500 response.
    """
    logger.error(f"Global Exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Internal Server Error. Please check server logs."},
    )

async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """
    Handler for standard HTTP exceptions (404, 401, 403, etc.).
    """
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
    )

async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """
    Handler for Pydantic validation errors (422).
    Returns a cleaner list of what fields failed validation.
    """
    errors = []
    for error in exc.errors():
        field = ".".join(str(x) for x in error["loc"]) if error["loc"] else "body"
        msg = error["msg"]
        errors.append(f"{field}: {msg}")
        
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": "Validation Error", "errors": errors},
    )