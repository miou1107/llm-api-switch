"""FastAPI middleware for auth, error handling, and request logging."""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from src.db.queries import validate_admin_session
from src.gateway.schemas import ErrorResponse

logger = logging.getLogger(__name__)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Log method, path, and status for every request."""

    async def dispatch(
        self, request: Request, call_next: Callable
    ) -> Response:
        start = time.monotonic()
        response = await call_next(request)
        elapsed_ms = (time.monotonic() - start) * 1000
        logger.info(
            "%s %s → %d (%.1fms)",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
        )
        return response


class ProxyKeyAuthMiddleware(BaseHTTPMiddleware):
    """Bearer token auth for /v1/* endpoints.

    Validates API keys against the database. Admin/dashboard paths are
    protected by AdminAuthMiddleware.
    """

    async def dispatch(
        self, request: Request, call_next: Callable
    ) -> Response:
        path = request.url.path

        # Only authenticate /v1/* paths
        if not path.startswith("/v1/"):
            return await call_next(request)

        # Skip health
        if path == "/health":
            return await call_next(request)

        # Extract auth header
        auth_header = request.headers.get("Authorization", "")

        # Extract Bearer token
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
        else:
            token = ""

        if not token:
            return JSONResponse(
                status_code=401,
                content={"error": {"message": "Missing API key. Use: Authorization: Bearer sk-xxx", "type": "authentication_error", "code": 401}},
            )

        # Validate against DB
        from src.db.queries import validate_api_key
        db = request.app.state.db
        key_record = await validate_api_key(db, token)

        if key_record is None:
            return JSONResponse(
                status_code=401,
                content={"error": {"message": "Invalid API key", "type": "authentication_error", "code": 401}},
            )

        # Store for downstream usage tracking
        request.state.api_key_record = key_record

        return await call_next(request)


class AdminAuthMiddleware(BaseHTTPMiddleware):
    """Cookie-based session auth for /admin/* and /dashboard/* endpoints."""

    # Paths that don't require authentication
    _PUBLIC_PATHS = {
        "/admin/auth/login",
        "/admin/auth/setup",
        "/admin/auth/logout",
        "/admin/auth/status",
    }

    async def dispatch(
        self, request: Request, call_next: Callable
    ) -> Response:
        path = request.url.path

        # Only intercept /admin/* API paths (dashboard static files
        # are served directly — the frontend JS handles auth checks)
        if not path.startswith("/admin/"):
            return await call_next(request)

        # Skip auth for public endpoints
        if path in self._PUBLIC_PATHS:
            return await call_next(request)

        # Read session cookie and validate
        token = request.cookies.get("admin_session")
        if token:
            db = request.app.state.db
            user = await validate_admin_session(db, token)
            if user:
                request.state.admin_user = user
                return await call_next(request)

        # Not authenticated
        return JSONResponse(
            status_code=401,
            content={"error": "Not authenticated"},
        )


def register_error_handlers(app: FastAPI) -> None:
    """Register exception handlers that return OpenAI-compatible errors."""

    @app.exception_handler(ValueError)
    async def value_error_handler(
        request: Request, exc: ValueError
    ) -> JSONResponse:
        error = ErrorResponse(
            error={
                "message": str(exc),
                "type": "invalid_request_error",
                "code": 400,
            }
        )
        return JSONResponse(status_code=400, content=error.model_dump())

    @app.exception_handler(RuntimeError)
    async def runtime_error_handler(
        request: Request, exc: RuntimeError
    ) -> JSONResponse:
        error = ErrorResponse(
            error={
                "message": str(exc),
                "type": "server_error",
                "code": 502,
            }
        )
        return JSONResponse(status_code=502, content=error.model_dump())

    @app.exception_handler(Exception)
    async def general_error_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        logger.exception("Unhandled exception: %s", exc)
        error = ErrorResponse(
            error={
                "message": "Internal server error",
                "type": "server_error",
                "code": 500,
            }
        )
        return JSONResponse(status_code=500, content=error.model_dump())
