"""Health checker — pings providers and evaluates output quality."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiosqlite
import httpx

from src.db.queries import record_health_check
from src.pool.manager import PoolManager
from src.pool.provider import ModelConfig, ProviderConfig

logger = logging.getLogger(__name__)

# Quality-check reference prompt and expected keywords.
_QUALITY_PROMPT = "Explain what a binary search tree is in exactly 3 sentences."
_QUALITY_KEYWORDS = [
    "binary",
    "search",
    "tree",
    "node",
    "left",
    "right",
    "sorted",
    "order",
]


def _classify_error(exc: BaseException) -> str:
    """Map an exception to a short error_type tag."""
    msg = str(exc).lower()
    if isinstance(exc, asyncio.TimeoutError) or "timeout" in msg:
        return "timeout"
    if "402" in msg or "payment" in msg or "insufficient" in msg or "quota" in msg:
        return "no_balance"
    if "rate" in msg or "429" in msg:
        return "rate_limit"
    if "auth" in msg or "401" in msg or "403" in msg or "invalid api key" in msg:
        return "auth"
    if "500" in msg or "502" in msg or "503" in msg or "server" in msg:
        return "server_error"
    return "unknown"


class HealthChecker:
    """Sends lightweight probes to every enabled provider/model."""

    def __init__(self, pool_manager: PoolManager, db: aiosqlite.Connection, settings: dict) -> None:
        self.pool = pool_manager
        self.db = db
        self.settings = settings

    # ------------------------------------------------------------------
    # Ping (latency & availability)
    # ------------------------------------------------------------------

    @staticmethod
    def _api_url(provider: ProviderConfig) -> str:
        return f"{provider.base_url.rstrip('/')}/chat/completions"

    @staticmethod
    def _api_headers(provider: ProviderConfig) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        api_key = provider.api_key
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    async def ping_check(
        self, provider: ProviderConfig, model: ModelConfig
    ) -> dict[str, Any]:
        """Send a minimal completion and record latency / success."""
        url = self._api_url(provider)
        headers = self._api_headers(provider)
        payload = {
            "model": model.id,
            "messages": [{"role": "user", "content": "Say hi"}],
            "max_tokens": 5,
        }
        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
            latency_ms = (time.monotonic() - start) * 1000
            result: dict[str, Any] = {
                "provider_id": provider.id,
                "model_id": model.id,
                "success": True,
                "latency_ms": round(latency_ms, 1),
                "error_type": None,
                "quality_score": None,
                "tokens_used": None,
            }
        except Exception as exc:  # noqa: BLE001
            latency_ms = (time.monotonic() - start) * 1000
            error_type = _classify_error(exc)
            result = {
                "provider_id": provider.id,
                "model_id": model.id,
                "success": False,
                "latency_ms": round(latency_ms, 1),
                "error_type": error_type,
                "quality_score": None,
                "tokens_used": None,
            }
            logger.warning(
                "ping failed provider=%s model=%s error=%s",
                provider.id,
                model.id,
                error_type,
            )

        await record_health_check(self.db, **result)
        return result

    async def ping_all(self) -> list[dict[str, Any]]:
        """Run ping checks on every enabled provider/model concurrently."""
        tasks: list[asyncio.Task] = []
        for provider in self.pool.get_enabled_providers():
            for model in provider.models:
                tasks.append(
                    asyncio.ensure_future(self.ping_check(provider, model))
                )
        if not tasks:
            return []
        results = await asyncio.gather(*tasks, return_exceptions=True)
        output: list[dict[str, Any]] = []
        for r in results:
            if isinstance(r, BaseException):
                logger.error("Unexpected error during ping_all: %s", r)
            else:
                output.append(r)
        return output

    # ------------------------------------------------------------------
    # Quality check
    # ------------------------------------------------------------------

    def _score_quality(self, text: str) -> float:
        """Heuristic 0.0-1.0 quality score for a quality-check response."""
        if not text or not text.strip():
            return 0.0

        score = 0.0
        text_lower = text.lower()

        # 1. Length: penalise very short or very long answers.
        word_count = len(text.split())
        if 10 <= word_count <= 200:
            score += 0.3
        elif word_count > 200:
            score += 0.15
        else:
            score += 0.05

        # 2. Keyword relevance.
        hits = sum(1 for kw in _QUALITY_KEYWORDS if kw in text_lower)
        score += min(hits / len(_QUALITY_KEYWORDS), 1.0) * 0.4

        # 3. Sentence count (target: exactly 3).
        sentences = [s.strip() for s in text.replace("!", ".").replace("?", ".").split(".") if s.strip()]
        if len(sentences) == 3:
            score += 0.3
        elif 2 <= len(sentences) <= 5:
            score += 0.15
        else:
            score += 0.05

        return round(min(score, 1.0), 3)

    async def quality_check(
        self, provider: ProviderConfig, model: ModelConfig
    ) -> dict[str, Any]:
        """Send a standardised prompt and score the output quality."""
        url = self._api_url(provider)
        headers = self._api_headers(provider)
        payload = {
            "model": model.id,
            "messages": [{"role": "user", "content": _QUALITY_PROMPT}],
            "max_tokens": 300,
        }
        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
            data = resp.json()
            latency_ms = (time.monotonic() - start) * 1000
            content = data["choices"][0]["message"].get("content", "") or ""
            quality = self._score_quality(content)

            result: dict[str, Any] = {
                "provider_id": provider.id,
                "model_id": model.id,
                "success": True,
                "latency_ms": round(latency_ms, 1),
                "error_type": None,
                "quality_score": quality,
                "tokens_used": None,
            }
        except Exception as exc:  # noqa: BLE001
            latency_ms = (time.monotonic() - start) * 1000
            error_type = _classify_error(exc)
            result = {
                "provider_id": provider.id,
                "model_id": model.id,
                "success": False,
                "latency_ms": round(latency_ms, 1),
                "error_type": error_type,
                "quality_score": 0.0,
                "tokens_used": None,
            }
            logger.warning(
                "quality check failed provider=%s model=%s error=%s",
                provider.id,
                model.id,
                error_type,
            )

        await record_health_check(self.db, **result)
        return result

    async def quality_check_all(self) -> list[dict[str, Any]]:
        """Run quality checks on every enabled provider/model concurrently."""
        tasks: list[asyncio.Task] = []
        for provider in self.pool.get_enabled_providers():
            for model in provider.models:
                tasks.append(
                    asyncio.ensure_future(self.quality_check(provider, model))
                )
        if not tasks:
            return []
        results = await asyncio.gather(*tasks, return_exceptions=True)
        output: list[dict[str, Any]] = []
        for r in results:
            if isinstance(r, BaseException):
                logger.error("Unexpected error during quality_check_all: %s", r)
            else:
                output.append(r)
        return output
