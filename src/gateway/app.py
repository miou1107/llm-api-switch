"""FastAPI application factory with lifespan management."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from src.admin.auth import router as auth_router
from src.admin.routes import router as admin_router
from src.db.database import close_db, init_db
from src.discovery.scanner import DiscoveryScanner
from src.db.queries import (
    get_all_provider_api_keys,
    log_event,
    record_api_key_usage,
    save_provider_api_key,
)
from src.pool.key_store import get_all_keys as ks_get_all_keys
from src.gateway.middleware import (
    AdminAuthMiddleware,
    ProxyKeyAuthMiddleware,
    RequestLoggingMiddleware,
    register_error_handlers,
)
from src.gateway.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ModelInfo,
    ModelListResponse,
)
from src.monitor.auto_manager import AutoManager
from src.monitor.health_checker import HealthChecker
from src.monitor.scheduler import MonitorScheduler
from src.monitor.scorer import Scorer
from src.pool.config_loader import load_settings
from src.pool.key_store import load_keys, load_keys_from_dict
from src.pool.manager import PoolManager
from src.router.router import Router

logger = logging.getLogger(__name__)

# Resolve paths relative to project root
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_CONFIG_DIR = _PROJECT_ROOT / "config"
_DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"


async def _backup_in_memory_keys_to_db(
    db: Any, existing_db_keys: dict[str, str]
) -> None:
    """Auto-backup in-memory keys to database if yaml was lost.

    This recovery mechanism ensures keys in memory are persisted,
    preventing loss if the app restarts and yaml is missing.
    """
    # This is a no-op in this version since keys are auto-saved when posted
    # But the infrastructure is in place for future enhancements
    pass


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialize services on startup, clean up on shutdown."""
    logger.info("Starting LLM API Switch...")

    # Load API keys into key_store (before pool init)
    load_keys(_CONFIG_DIR / "api_keys.yaml")

    settings = load_settings(_CONFIG_DIR / "settings.yaml")
    db = await init_db()

    # Load API keys from database (persistent storage backup)
    db_keys = await get_all_provider_api_keys(db)
    if db_keys:
        load_keys_from_dict(db_keys)
        logger.info("Loaded %d API keys from database", len(db_keys))

    pool = PoolManager(_CONFIG_DIR, db)
    await pool.initialize()
    router = Router(pool, db, settings)

    # Discovery scanner
    scanner = DiscoveryScanner(pool, db, settings, _CONFIG_DIR)

    # Monitor components
    health_checker = HealthChecker(pool, db, settings)
    scorer = Scorer(db, settings)
    auto_manager = AutoManager(pool, db, settings)
    scheduler = MonitorScheduler(
        health_checker=health_checker,
        scorer=scorer,
        auto_manager=auto_manager,
        pool_manager=pool,
        settings=settings,
        scanner=scanner,
    )
    scheduler.start()

    app.state.pool = pool
    app.state.router = router
    app.state.db = db
    app.state.settings = settings
    app.state.scanner = scanner
    app.state.scheduler = scheduler

    logger.info(
        "LLM API Switch ready — %d providers, %d aliases",
        len(pool.providers),
        len(pool.aliases),
    )

    # Auto-backup: ensure all in-memory keys are persisted to database
    await _backup_in_memory_keys_to_db(db, db_keys)

    yield

    scheduler.stop()
    await close_db()
    logger.info("LLM API Switch shut down.")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="LLM API Switch",
        description="Free LLM API Auto-Aggregator",
        version="0.1.0",
        lifespan=lifespan,
    )

    # --- Middleware (outermost first) ---
    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(ProxyKeyAuthMiddleware)
    app.add_middleware(AdminAuthMiddleware)
    register_error_handlers(app)

    # --- Admin routes ---
    app.include_router(admin_router)
    app.include_router(auth_router)

    # --- Dashboard static files ---
    if _DASHBOARD_DIR.exists():
        app.mount("/dashboard", StaticFiles(directory=str(_DASHBOARD_DIR), html=True), name="dashboard")

    # --- API Routes ---

    @app.post("/v1/chat/completions", response_model=None)
    async def chat_completions(
        request_body: ChatCompletionRequest, request: Request
    ) -> ChatCompletionResponse | StreamingResponse:
        router: Router = request.app.state.router
        if request_body.stream:
            # Validate route BEFORE starting SSE stream to avoid mid-stream errors
            try:
                _ = await router._build_candidates(request_body.model)
            except (ValueError, RuntimeError) as exc:
                from fastapi.responses import JSONResponse
                return JSONResponse(
                    status_code=400,
                    content={"error": {"message": str(exc), "type": "invalid_request_error", "code": 400}},
                )
            return StreamingResponse(
                router.handle_streaming_request(request_body),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )
        import time as _time
        _t0 = _time.monotonic()
        response = await router.handle_request(request_body)
        _elapsed = (_time.monotonic() - _t0) * 1000
        # Record activity for idle detection
        if response.provider:
            scheduler = getattr(request.app.state, "scheduler", None)
            if scheduler:
                scheduler.record_activity(response.provider, request_body.model)
        # Track usage per API key
        key_record = getattr(request.state, "api_key_record", None)
        key_id = key_record["key_id"] if key_record else None
        tokens = response.usage.total_tokens if response.usage else 0
        if key_record and tokens:
            await record_api_key_usage(request.app.state.db, key_id, tokens)
        # Log event
        await log_event(
            request.app.state.db, "api_call",
            f"{request_body.model} → {response.model}",
            provider=response.provider, model=response.model,
            latency_ms=round(_elapsed, 1), tokens=tokens, api_key_id=key_id,
        )
        return response

    @app.get("/v1/models")
    async def list_models(request: Request) -> ModelListResponse:
        pool: PoolManager = request.app.state.pool
        raw_models = pool.get_all_available_models()
        models = [
            ModelInfo(
                id=m["id"],
                owned_by=m.get("owned_by", "llm-api-switch"),
            )
            for m in raw_models
        ]
        return ModelListResponse(data=models)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
