"""ModelSync — periodically checks each provider's /v1/models and updates available models."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from src.pool.manager import PoolManager
from src.pool.provider import ModelConfig, RateLimits

logger = logging.getLogger(__name__)

# Default rate limits for auto-discovered models
_DEFAULT_LIMITS = RateLimits(rpm=10, rpd=1000, tpm=100000)


async def sync_all_models(pool: PoolManager) -> dict[str, Any]:
    """Query each provider's /v1/models and update the pool.

    Returns summary: {provider_id: {added: [...], removed: [...]}}
    """
    results: dict[str, Any] = {}

    for provider in pool.get_all_providers():
        if not provider.has_api_key:
            continue

        try:
            remote_models = await _fetch_models(provider)
            if remote_models is None:
                continue

            current_ids = {m.id for m in provider.models}
            remote_ids = set(remote_models)

            added = remote_ids - current_ids
            removed = current_ids - remote_ids

            changed = False

            # Add new models
            for model_id in added:
                new_model = ModelConfig(
                    id=model_id,
                    context_window=8192,
                    max_output_tokens=4096,
                    supports_streaming=True,
                    supports_function_calling=False,
                    rate_limits=_DEFAULT_LIMITS,
                    source="auto-discovered",
                )
                provider.models.append(new_model)
                changed = True
                logger.info("Model added: %s/%s", provider.id, model_id)

            # Remove models that no longer exist
            if removed:
                provider.models = [m for m in provider.models if m.id not in removed]
                changed = True
                for mid in removed:
                    logger.info("Model removed: %s/%s", provider.id, mid)

            if changed:
                await pool._save()

            if added or removed:
                results[provider.id] = {
                    "added": list(added),
                    "removed": list(removed),
                }

        except Exception as exc:
            logger.warning("Model sync failed for %s: %s", provider.id, exc)

    if results:
        logger.info("Model sync complete: %s", results)
    else:
        logger.debug("Model sync: no changes")

    return results


async def _fetch_models(provider) -> set[str] | None:
    """Fetch model list from a provider's /v1/models endpoint."""
    api_key = provider.api_key
    if not api_key:
        return None

    # Build URL
    base = provider.base_url.rstrip("/")
    # Some providers need /v1/models, some already have /v1 in base_url
    if base.endswith("/v1"):
        url = base + "/models"
    else:
        url = base + "/v1/models"

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                url,
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if resp.status_code != 200:
                logger.debug("Model list failed for %s: HTTP %d", provider.id, resp.status_code)
                return None

            data = resp.json()
            models = data.get("data", [])
            return {m["id"] for m in models if isinstance(m, dict) and "id" in m}

    except Exception as exc:
        logger.debug("Model list request failed for %s: %s", provider.id, exc)
        return None
