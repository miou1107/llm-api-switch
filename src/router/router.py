"""Router — selects providers and handles requests with fallback."""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, AsyncIterator

import aiosqlite
import httpx

from src.db.queries import (
    get_provider_score,
    record_health_check,
    record_quota_usage,
)
from src.gateway.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    Choice,
    Usage,
)
from src.pool.manager import PoolManager
from src.pool.provider import ProviderConfig
from src.pool.quota_tracker import QuotaTracker
from src.router.fallback import FallbackChain
from src.router.strategies import STRATEGIES, ScoredCandidate

logger = logging.getLogger(__name__)


class Router:
    """Selects the best provider+model for a request and executes it."""

    def __init__(
        self,
        pool_manager: PoolManager,
        db: aiosqlite.Connection,
        settings: dict[str, Any],
    ) -> None:
        self.pool = pool_manager
        self.db = db
        self.settings = settings
        self.quota_tracker = QuotaTracker(db)

        routing_cfg = settings.get("routing", {})
        self.strategy_name: str = routing_cfg.get("strategy", "best_score")
        self.max_fallback_attempts: int = routing_cfg.get("max_fallback_attempts", 3)

    async def _build_candidates(
        self, model_name: str
    ) -> list[ScoredCandidate]:
        """Resolve model name and score each candidate."""
        # "auto" = pick from ALL enabled providers/models
        if model_name == "auto":
            raw_candidates = []
            for provider in self.pool.get_enabled_providers():
                for model in provider.models:
                    raw_candidates.append((provider, model))
        else:
            raw_candidates = self.pool.resolve_model(model_name)
        if not raw_candidates:
            raise ValueError(f"No providers found for model: {model_name}")

        scored: list[ScoredCandidate] = []
        for provider, model in raw_candidates:
            provider_id = provider.id
            model_id = model.id

            # Check quota — skip if exhausted (<10% remaining)
            quota_remaining = await self.quota_tracker.check_quota(
                provider_id, model_id, model.rate_limits
            )
            if quota_remaining < 0.10:
                logger.debug(
                    "Skipping %s/%s — quota exhausted (%.1f%%)",
                    provider_id, model_id, quota_remaining * 100,
                )
                continue

            # Fetch scores from DB
            score_row = await get_provider_score(self.db, provider_id, model_id)

            if score_row:
                composite = score_row.get("composite_score", 0.5)
            else:
                # Default score for new/unknown providers
                composite = 0.5

            scored.append(
                ScoredCandidate(
                    provider_id=provider_id,
                    model_id=model_id,
                    provider_config=provider,
                    model_config=model,
                    composite_score=composite,
                )
            )

        if not scored:
            raise ValueError(
                f"All providers for model '{model_name}' are disabled or quota-exhausted"
            )

        # Sort by score descending (best first for fallback ordering)
        scored.sort(key=lambda s: s.composite_score, reverse=True)
        return scored

    async def route(
        self, request: ChatCompletionRequest
    ) -> tuple[ProviderConfig, str]:
        """Select best provider+model for the request.

        Returns (provider_config, model_id).
        """
        candidates = await self._build_candidates(request.model)

        strategy_fn = STRATEGIES.get(self.strategy_name)
        if strategy_fn is None:
            raise ValueError(f"Unknown routing strategy: {self.strategy_name}")

        selected = await strategy_fn(candidates, counter_key=request.model)
        return selected.provider_config, selected.model_id

    def _build_payload(
        self, model_id: str, request: ChatCompletionRequest
    ) -> dict[str, Any]:
        """Build the OpenAI-compatible JSON payload."""
        payload: dict[str, Any] = {
            "model": model_id,
            "messages": [m.model_dump(exclude_none=True) for m in request.messages],
            "stream": request.stream,
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.top_p is not None:
            payload["top_p"] = request.top_p
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.stop is not None:
            payload["stop"] = request.stop
        if request.tools is not None:
            payload["tools"] = request.tools
        if request.tool_choice is not None:
            payload["tool_choice"] = request.tool_choice
        return payload

    @staticmethod
    def _provider_headers(provider: ProviderConfig) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        api_key = provider.api_key
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    @staticmethod
    def _provider_url(provider: ProviderConfig) -> str:
        return f"{provider.base_url.rstrip('/')}/chat/completions"

    async def call_provider(
        self,
        provider: ProviderConfig,
        model_id: str,
        request: ChatCompletionRequest,
    ) -> Any:
        """Call a provider via httpx (OpenAI-compatible endpoint)."""
        url = self._provider_url(provider)
        headers = self._provider_headers(provider)
        payload = self._build_payload(model_id, request)

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            return resp.json()

    async def handle_request(
        self, request: ChatCompletionRequest
    ) -> ChatCompletionResponse:
        """Full request handling with fallback (non-streaming)."""
        candidates = await self._build_candidates(request.model)

        chain = FallbackChain(max_attempts=self.max_fallback_attempts)

        async def _call(
            provider: ProviderConfig, model_id: str
        ) -> Any:
            return await self.call_provider(provider, model_id, request)

        start_time = time.monotonic()

        try:
            response = await chain.execute(
                candidates,
                _call,
            )
        except RuntimeError:
            for failed in chain.failed_candidates:
                await record_health_check(
                    self.db,
                    provider_id=failed.provider_id,
                    model_id=failed.model_id,
                    latency_ms=0,
                    success=False,
                    error_type="all_attempts_failed",
                )
            raise

        elapsed_ms = (time.monotonic() - start_time) * 1000
        winner = chain.successful_candidate

        if winner:
            usage = response.get("usage") or {}
            tokens_used = usage.get("total_tokens", 0)

            await record_health_check(
                self.db,
                provider_id=winner.provider_id,
                model_id=winner.model_id,
                latency_ms=elapsed_ms,
                success=True,
                tokens_used=tokens_used,
            )
            if tokens_used:
                await record_quota_usage(
                    self.db,
                    provider_id=winner.provider_id,
                    model_id=winner.model_id,
                    tokens_consumed=tokens_used,
                )

            for failed in chain.failed_candidates:
                await record_health_check(
                    self.db,
                    provider_id=failed.provider_id,
                    model_id=failed.model_id,
                    latency_ms=0,
                    success=False,
                    error_type="fallback_skipped",
                )

        return self._to_response(response, request.model, winner)

    async def _call_provider_stream(
        self,
        provider: ProviderConfig,
        model_id: str,
        request: ChatCompletionRequest,
    ) -> httpx.Response:
        """Start a streaming request; returns the httpx Response (caller iterates)."""
        url = self._provider_url(provider)
        headers = self._provider_headers(provider)
        payload = self._build_payload(model_id, request)

        client = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10))
        try:
            resp = await client.send(
                client.build_request("POST", url, json=payload, headers=headers),
                stream=True,
            )
            resp.raise_for_status()
        except Exception:
            await client.aclose()
            raise
        # Attach client so we can close later
        resp._client = client  # type: ignore[attr-defined]
        return resp

    async def handle_streaming_request(
        self, request: ChatCompletionRequest
    ) -> AsyncIterator[str]:
        """Full request handling for streaming — yields SSE lines."""
        candidates = await self._build_candidates(request.model)

        chain = FallbackChain(max_attempts=self.max_fallback_attempts)

        async def _call(
            provider: ProviderConfig, model_id: str
        ) -> Any:
            return await self._call_provider_stream(provider, model_id, request)

        start_time = time.monotonic()
        resp: httpx.Response = await chain.execute(candidates, _call)
        winner = chain.successful_candidate

        try:
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line:
                    continue
                # Forward SSE lines as-is from upstream
                if line.startswith("data:"):
                    yield f"{line}\n\n"
                # Some providers send lines without "data:" prefix
                elif line.startswith("{"):
                    yield f"data: {line}\n\n"
        finally:
            await resp.aclose()
            client = getattr(resp, "_client", None)
            if client:
                await client.aclose()

        elapsed_ms = (time.monotonic() - start_time) * 1000
        if winner:
            await record_health_check(
                self.db,
                provider_id=winner.provider_id,
                model_id=winner.model_id,
                latency_ms=elapsed_ms,
                success=True,
            )

    def _to_response(
        self,
        raw: dict[str, Any],
        requested_model: str,
        winner: ScoredCandidate | None,
    ) -> ChatCompletionResponse:
        """Convert raw JSON dict to our schema."""
        choices: list[Choice] = []
        for c in raw.get("choices", []):
            msg = c.get("message", {})
            choices.append(
                Choice(
                    index=c.get("index", 0),
                    message=ChatMessage(
                        role=msg.get("role", "assistant"),
                        content=msg.get("content"),
                        tool_calls=msg.get("tool_calls"),
                    ),
                    finish_reason=c.get("finish_reason"),
                )
            )

        usage = None
        raw_usage = raw.get("usage")
        if raw_usage:
            usage = Usage(
                prompt_tokens=raw_usage.get("prompt_tokens", 0),
                completion_tokens=raw_usage.get("completion_tokens", 0),
                total_tokens=raw_usage.get("total_tokens", 0),
            )

        actual_model = winner.model_id if winner else requested_model

        return ChatCompletionResponse(
            id=raw.get("id", f"chatcmpl-{uuid.uuid4().hex[:24]}"),
            created=raw.get("created", int(time.time())),
            model=actual_model,
            choices=choices,
            usage=usage,
            provider=winner.provider_id if winner else None,
        )
