"""Admin API routes for managing providers, viewing scores, and triggering scans."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from datetime import datetime, timedelta, timezone

from src.db.queries import (
    create_api_key,
    delete_provider_api_key,
    get_all_provider_api_keys,
    get_health_checks_paginated,
    get_pending_discoveries,
    get_provider_scores,
    get_quota_usage_since,
    get_recent_health_checks,
    get_recent_logs,
    list_proxy_api_keys,
    log_event,
    revoke_api_key,
    save_provider_api_key,
    update_discovery_status,
)
from src.pool.key_store import add_key as store_add_key, get_all_keys, get_key_count
from src.pool.manager import PoolManager
from src.pool.provider import ModelConfig, ProviderConfig, RateLimits

# Registration links for known providers
_PROVIDER_SETUP_INFO: dict[str, dict[str, str]] = {
    "GROQ_API_KEY": {
        "provider": "Groq",
        "url": "https://console.groq.com/keys",
        "note": "14,400 req/day, 最快速度",
    },
    "CEREBRAS_API_KEY": {
        "provider": "Cerebras",
        "url": "https://cloud.cerebras.ai/platform",
        "note": "1M tokens/day",
    },
    "GEMINI_API_KEY": {
        "provider": "Google Gemini",
        "url": "https://aistudio.google.com/apikey",
        "note": "1,500 req/day, 1M tokens/day",
    },
    "MISTRAL_API_KEY": {
        "provider": "Mistral",
        "url": "https://console.mistral.ai/api-keys",
        "note": "1B tokens/month",
    },
    "OPENROUTER_API_KEY": {
        "provider": "OpenRouter",
        "url": "https://openrouter.ai/settings/keys",
        "note": "29+ 免費 model, 200 req/day",
    },
    "NVIDIA_API_KEY": {
        "provider": "NVIDIA NIM",
        "url": "https://build.nvidia.com/settings/api-keys",
        "note": "1000 免費 credits",
    },
    "DEEPSEEK_API_KEY": {
        "provider": "DeepSeek",
        "url": "https://platform.deepseek.com/api_keys",
        "note": "推理最強, 每月送 credits",
    },
    "TOGETHER_API_KEY": {
        "provider": "Together AI",
        "url": "https://api.together.xyz/settings/api-keys",
        "note": "200+ models, 免費 credits",
    },
    "SAMBANOVA_API_KEY": {
        "provider": "SambaNova",
        "url": "https://cloud.sambanova.ai",
        "note": "200K tokens/day 持久免費",
    },
    "GITHUB_TOKEN": {
        "provider": "GitHub Models",
        "url": "https://github.com/settings/tokens",
        "note": "50-150 req/day, 含 GPT-4o",
    },
    "SCALEWAY_API_KEY": {
        "provider": "Scaleway",
        "url": "https://console.scaleway.com/generative-apis/api-keys",
        "note": "1M 免費 tokens",
    },
}

_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"

router = APIRouter(prefix="/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class AddProviderRequest(BaseModel):
    id: str
    name: str
    base_url: str
    api_key_env: str | None = None
    litellm_provider: str | None = None
    enabled: bool = True
    models: list[dict[str, Any]] = []


class ProviderResponse(BaseModel):
    id: str
    name: str
    base_url: str
    litellm_provider: str | None
    source: str
    enabled: bool
    disable_reason: str | None = None
    model_count: int


class DiscoveryAction(BaseModel):
    discovery_id: int
    action: str  # "approve" | "reject"
    rejection_reason: str | None = None


# ---------------------------------------------------------------------------
# Provider management
# ---------------------------------------------------------------------------


@router.get("/providers")
async def list_providers(request: Request) -> list[ProviderResponse]:
    pool: PoolManager = request.app.state.pool
    return [
        ProviderResponse(
            id=p.id,
            name=p.name,
            base_url=p.base_url,
            litellm_provider=p.litellm_provider,
            source=p.source,
            enabled=p.enabled,
            disable_reason=p.disable_reason,
            model_count=len(p.models),
        )
        for p in pool.get_all_providers()
    ]


@router.post("/providers")
async def add_provider(body: AddProviderRequest, request: Request) -> ProviderResponse:
    pool: PoolManager = request.app.state.pool

    models = [
        ModelConfig(**m) for m in body.models
    ] if body.models else []

    config = ProviderConfig(
        id=body.id,
        name=body.name,
        base_url=body.base_url,
        api_key_env=body.api_key_env,
        litellm_provider=body.litellm_provider,
        source="manual",
        enabled=body.enabled,
        models=models,
    )
    await pool.add_provider(config)

    return ProviderResponse(
        id=config.id,
        name=config.name,
        base_url=config.base_url,
        litellm_provider=config.litellm_provider,
        source=config.source,
        enabled=config.enabled,
        model_count=len(config.models),
    )


@router.post("/providers/{provider_id}/enable")
async def enable_provider(provider_id: str, request: Request) -> dict[str, Any]:
    pool: PoolManager = request.app.state.pool
    found = await pool.enable_provider(provider_id)
    if not found:
        return {"error": f"Provider '{provider_id}' not found"}
    return {"status": "enabled", "provider_id": provider_id}


@router.post("/providers/{provider_id}/disable")
async def disable_provider(provider_id: str, request: Request) -> dict[str, Any]:
    pool: PoolManager = request.app.state.pool
    found = await pool.disable_provider(provider_id)
    if not found:
        return {"error": f"Provider '{provider_id}' not found"}
    return {"status": "disabled", "provider_id": provider_id}


# ---------------------------------------------------------------------------
# Scores
# ---------------------------------------------------------------------------


@router.get("/scores")
async def list_scores(request: Request) -> list[dict[str, Any]]:
    db = request.app.state.db
    return await get_provider_scores(db)


# ---------------------------------------------------------------------------
# Discoveries
# ---------------------------------------------------------------------------


@router.get("/discoveries/pending")
async def list_pending_discoveries(request: Request) -> list[dict[str, Any]]:
    db = request.app.state.db
    return await get_pending_discoveries(db)


@router.post("/discoveries/action")
async def handle_discovery_action(body: DiscoveryAction, request: Request) -> dict[str, Any]:
    db = request.app.state.db
    pool: PoolManager = request.app.state.pool

    if body.action == "approve":
        await update_discovery_status(db, body.discovery_id, "approved")
        return {"status": "approved", "discovery_id": body.discovery_id}
    elif body.action == "reject":
        await update_discovery_status(
            db, body.discovery_id, "rejected", body.rejection_reason
        )
        return {"status": "rejected", "discovery_id": body.discovery_id}
    else:
        return {"error": f"Unknown action: {body.action}"}


# ---------------------------------------------------------------------------
# Health check history (paginated)
# ---------------------------------------------------------------------------


@router.get("/health-checks")
async def list_health_checks(
    request: Request,
    page: int = 1,
    per_page: int = 50,
    provider: str | None = None,
    model: str | None = None,
    success: bool | None = None,
    error_type: str | None = None,
) -> dict[str, Any]:
    """Return paginated health check history with filters."""
    db = request.app.state.db
    per_page = min(per_page, 200)
    return await get_health_checks_paginated(
        db,
        page=page,
        per_page=per_page,
        provider_id=provider,
        model_id=model,
        success=success,
        error_type=error_type,
    )


# ---------------------------------------------------------------------------
# Stats & Scan
# ---------------------------------------------------------------------------


@router.get("/stats")
async def get_stats(request: Request) -> dict[str, Any]:
    pool: PoolManager = request.app.state.pool
    db = request.app.state.db
    all_providers = pool.get_all_providers()
    enabled = [p for p in all_providers if p.enabled and p.has_api_key]
    total_models = sum(len(p.models) for p in all_providers)
    auto_discovered = sum(1 for p in all_providers if p.source == "auto-discovered")

    # Total RPD pool (sum of all enabled models' daily limits)
    total_rpd = 0
    total_rpm = 0
    for p in enabled:
        for m in p.models:
            total_rpd += m.rate_limits.rpd
            total_rpm += m.rate_limits.rpm

    # Today's usage from event_log
    now = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    cursor = await db.execute(
        "SELECT COUNT(*) as calls, COALESCE(SUM(tokens),0) as tokens FROM event_log WHERE type='api_call' AND timestamp >= ?",
        (day_start,),
    )
    row = await cursor.fetchone()
    today_calls = row[0] if row else 0
    today_tokens = row[1] if row else 0

    # Average latency & success rate from recent health checks
    cursor2 = await db.execute(
        "SELECT AVG(latency_ms) as avg_lat, AVG(CASE WHEN success THEN 1.0 ELSE 0.0 END) as avg_sr FROM health_checks WHERE timestamp >= ?",
        (day_start,),
    )
    row2 = await cursor2.fetchone()
    avg_latency = round(row2[0], 1) if row2 and row2[0] else 0
    avg_success_rate = round(row2[1] * 100, 1) if row2 and row2[1] else 100

    # Today's quota used (all providers combined)
    cursor3 = await db.execute(
        "SELECT COALESCE(SUM(requests_count),0) FROM quota_usage WHERE timestamp >= ?",
        (day_start,),
    )
    row3 = await cursor3.fetchone()
    today_quota_used = row3[0] if row3 else 0

    return {
        "total_providers": len(all_providers),
        "enabled_providers": len(enabled),
        "total_models": total_models,
        "auto_discovered": auto_discovered,
        "total_rpd": total_rpd,
        "total_rpm": total_rpm,
        "today_calls": today_calls,
        "today_tokens": today_tokens,
        "today_quota_used": today_quota_used,
        "avg_latency_ms": avg_latency,
        "avg_success_rate": avg_success_rate,
    }


@router.get("/logs")
async def get_logs(request: Request, limit: int = 50, type: str | None = None) -> list[dict[str, Any]]:
    db = request.app.state.db
    return await get_recent_logs(db, limit=limit, event_type=type)


@router.post("/sync-models")
async def trigger_model_sync(request: Request) -> dict[str, Any]:
    """Manually trigger model sync across all providers."""
    from src.monitor.model_sync import sync_all_models
    pool: PoolManager = request.app.state.pool
    results = await sync_all_models(pool)
    return {"status": "completed", "changes": results}


@router.post("/health-check")
async def trigger_health_check(request: Request) -> dict[str, Any]:
    """Manually trigger health check + score update for all enabled providers."""
    scheduler: Any = request.app.state.scheduler
    health_checker = scheduler.health_checker
    scorer = scheduler.scorer
    pool: PoolManager = request.app.state.pool

    providers = pool.get_enabled_providers()
    checked = 0
    for provider in providers:
        for model in provider.models:
            await health_checker.ping_check(provider, model)
            checked += 1

    await scorer.update_all_scores(providers)
    return {"status": "completed", "checked": checked, "providers": len(providers)}


@router.post("/discovery/scan")
async def trigger_discovery_scan(request: Request) -> dict[str, Any]:
    scanner = getattr(request.app.state, "scanner", None)
    if scanner is None:
        return {"error": "Discovery scanner not initialized"}
    new_entries = await scanner.scan_all_sources()
    return {"status": "completed", "new_entries": len(new_entries)}


# ---------------------------------------------------------------------------
# Health details (per provider/model)
# ---------------------------------------------------------------------------


@router.get("/providers/detail")
async def list_providers_detail(request: Request) -> list[dict[str, Any]]:
    """Return providers with their models, scores, and recent health info."""
    pool: PoolManager = request.app.state.pool
    db = request.app.state.db
    result = []
    for p in pool.get_all_providers():
        models_detail = []
        for m in p.models:
            # Get score
            scores = await get_provider_scores(db)
            score_row = next(
                (s for s in scores if s["provider_id"] == p.id and s["model_id"] == m.id),
                None,
            )
            # Get last 5 health checks
            checks = await get_recent_health_checks(db, p.id, m.id, limit=5)
            # Get quota usage (last minute + last day)
            now = datetime.now(timezone.utc)
            minute_ago = (now - timedelta(minutes=1)).isoformat()
            day_ago = (now - timedelta(days=1)).isoformat()
            usage_minute = await get_quota_usage_since(db, p.id, m.id, minute_ago)
            usage_day = await get_quota_usage_since(db, p.id, m.id, day_ago)
            models_detail.append({
                "id": m.id,
                "context_window": m.context_window,
                "max_output_tokens": m.max_output_tokens,
                "supports_streaming": m.supports_streaming,
                "rate_limits": {"rpm": m.rate_limits.rpm, "rpd": m.rate_limits.rpd, "tpm": m.rate_limits.tpm},
                "quota": {
                    "minute": {"used_requests": usage_minute["total_requests"], "used_tokens": usage_minute["total_tokens"], "limit_rpm": m.rate_limits.rpm, "limit_tpm": m.rate_limits.tpm},
                    "day": {"used_requests": usage_day["total_requests"], "used_tokens": usage_day["total_tokens"], "limit_rpd": m.rate_limits.rpd},
                },
                "score": {
                    "composite": score_row["composite_score"] if score_row else None,
                    "success_rate": score_row["success_rate"] if score_row else None,
                    "latency_p50": score_row["latency_p50_ms"] if score_row else None,
                    "quality": score_row["quality_score"] if score_row else None,
                    "updated_at": score_row["updated_at"] if score_row else None,
                } if score_row else None,
                "recent_checks": [
                    {
                        "timestamp": c["timestamp"],
                        "success": bool(c["success"]),
                        "latency_ms": c["latency_ms"],
                        "error_type": c["error_type"],
                    }
                    for c in checks
                ],
            })
        result.append({
            "id": p.id,
            "name": p.name,
            "base_url": p.base_url,
            "litellm_provider": p.litellm_provider,
            "source": p.source,
            "enabled": p.enabled,
            "has_api_key": bool(p.api_key),
            "models": models_detail,
        })
    return result


# ---------------------------------------------------------------------------
# API Key management
# ---------------------------------------------------------------------------


class SetApiKeyRequest(BaseModel):
    env_name: str
    value: str


def _mask_key(key: str) -> str:
    """Mask an API key for display: show first 4 and last 4 chars."""
    if not key or len(key) <= 8:
        return "****"
    return key[:4] + "..." + key[-4:]


@router.get("/api-keys")
async def list_api_keys(request: Request) -> list[dict[str, Any]]:
    """Return API key status for all providers (masked, never full key)."""
    pool: PoolManager = request.app.state.pool
    result = []
    seen_envs: set[str] = set()

    for p in pool.get_all_providers():
        env_name = p.api_key_env
        if not env_name or env_name in seen_envs:
            continue
        seen_envs.add(env_name)

        keys = get_all_keys(env_name)
        info = _PROVIDER_SETUP_INFO.get(env_name, {})

        result.append({
            "env_name": env_name,
            "provider_id": p.id,
            "provider_name": p.name,
            "is_set": len(keys) > 0,
            "key_count": len(keys),
            "masked_values": [_mask_key(k) for k in keys],
            "register_url": info.get("url"),
            "free_tier_note": info.get("note"),
        })

    return result


@router.post("/api-keys")
async def set_api_key(body: SetApiKeyRequest, request: Request) -> dict[str, Any]:
    """Add an API key — saves to database (persistent), yaml, and key_store."""
    db = request.app.state.db
    api_keys_path = _CONFIG_DIR / "api_keys.yaml"

    # Save to database (primary persistent storage)
    await save_provider_api_key(db, body.env_name, body.value)

    # Also save to yaml for backward compatibility
    store_add_key(api_keys_path, body.env_name, body.value)

    return {
        "status": "saved",
        "env_name": body.env_name,
        "masked_value": _mask_key(body.value),
        "key_count": get_key_count(body.env_name),
    }


@router.post("/api-keys/backup-to-db")
async def backup_keys_to_database(request: Request) -> dict[str, Any]:
    """Emergency: backup all in-memory keys to database.

    Use this if yaml file is lost but keys are still in memory.
    Call once after app restart to persist them permanently.
    """
    db = request.app.state.db
    backed_up = 0

    # Get all current keys from key_store (in memory)
    # Iterate through all known env_names from pool providers
    pool: PoolManager = request.app.state.pool
    seen_envs: set[str] = set()

    for provider in pool.get_all_providers():
        env_name = provider.api_key_env
        if not env_name or env_name in seen_envs:
            continue
        seen_envs.add(env_name)

        keys = get_all_keys(env_name)
        for key in keys:
            if key:
                await save_provider_api_key(db, env_name, key, provider.id)
                backed_up += 1

    return {
        "status": "backed_up",
        "keys_saved": backed_up,
        "message": f"Successfully backed up {backed_up} key(s) to database",
    }


# ---------------------------------------------------------------------------
# Proxy API Keys (for external users)
# ---------------------------------------------------------------------------


class ChatTestRequest(BaseModel):
    model: str
    message: str


@router.post("/chat-test")
async def admin_chat_test(body: ChatTestRequest, request: Request) -> dict[str, Any]:
    """Test chat from Dashboard (no Bearer auth needed, protected by nginx Basic Auth)."""
    import time as _time
    from src.gateway.schemas import ChatCompletionRequest, ChatMessage
    router_obj: Any = request.app.state.router
    db = request.app.state.db
    chat_req = ChatCompletionRequest(
        model=body.model,
        messages=[ChatMessage(role="user", content=body.message)],
    )
    t0 = _time.monotonic()
    try:
        response = await router_obj.handle_request(chat_req)
        elapsed = (_time.monotonic() - t0) * 1000
        content = response.choices[0].message.content if response.choices else ""
        tokens = response.usage.total_tokens if response.usage else 0
        await log_event(db, "api_call", f"{body.model} → {response.model}",
                        provider=response.provider, model=response.model,
                        latency_ms=round(elapsed, 1), tokens=tokens)
        return {
            "content": content,
            "tokens": tokens,
            "model": response.model,
            "provider": response.provider,
        }
    except Exception as exc:
        elapsed = (_time.monotonic() - t0) * 1000
        await log_event(db, "api_call", f"{body.model} failed",
                        model=body.model, latency_ms=round(elapsed, 1), error=str(exc)[:200])
        return {"error": str(exc)}


class CreateProxyKeyRequest(BaseModel):
    name: str
    rate_limit_rpm: int = 30


@router.get("/proxy-keys")
async def list_proxy_keys(request: Request) -> list[dict[str, Any]]:
    db = request.app.state.db
    return await list_proxy_api_keys(db)


@router.post("/proxy-keys")
async def create_proxy_key(body: CreateProxyKeyRequest, request: Request) -> dict[str, Any]:
    db = request.app.state.db
    result = await create_api_key(db, body.name, body.rate_limit_rpm)
    return result


@router.post("/proxy-keys/{key_id}/revoke")
async def revoke_proxy_key(key_id: str, request: Request) -> dict[str, Any]:
    db = request.app.state.db
    found = await revoke_api_key(db, key_id)
    if not found:
        return {"error": f"Key '{key_id}' not found"}
    return {"status": "revoked", "key_id": key_id}
