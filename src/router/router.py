"""Router — selects providers and handles requests with fallback."""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, AsyncIterator

import aiosqlite
import litellm

from src.db.queries import (
    get_provider_score,
    record_health_check,
    record_quota_usage,
)
from src.gateway.schemas import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    Choice,
    StreamChoice,
    Usage,
)
from src.pool.manager import PoolManager
from src.pool.provider import ModelConfig, ProviderConfig, RateLimits
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

    def _build_litellm_model(
        self, provider: ProviderConfig, model_id: str
    ) -> str:
        """Build the litellm model string."""
        if provider.litellm_provider:
            if model_id.startswith(f"{provider.litellm_provider}/"):
                return model_id
            return f"{provider.litellm_provider}/{model_id}"
        return model_id

    async def call_provider(
        self,
        provider: ProviderConfig,
        model_id: str,
        request: ChatCompletionRequest,
    ) -> Any:
        """Call a provider via litellm.acompletion()."""
        litellm_model = self._build_litellm_model(provider, model_id)

        kwargs: dict[str, Any] = {
            "model": litellm_model,
            "messages": [m.model_dump(exclude_none=True) for m in request.messages],
            "stream": request.stream,
        }

        # Optional parameters
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if request.top_p is not None:
            kwargs["top_p"] = request.top_p
        if request.max_tokens is not None:
            kwargs["max_tokens"] = request.max_tokens
        if request.stop is not None:
            kwargs["stop"] = request.stop
        if request.tools is not None:
            kwargs["tools"] = request.tools
        if request.tool_choice is not None:
            kwargs["tool_choice"] = request.tool_choice

        # Provider auth
        api_key = provider.api_key
        if api_key:
            kwargs["api_key"] = api_key

        # Pass api_base for custom endpoints (even if litellm_provider is set)
        if provider.base_url:
            # Skip only for native litellm providers that don't need custom base_url
            native_urls = {"api.groq.com", "api.mistral.ai", "api.cerebras.ai",
                          "generativelanguage.googleapis.com", "api.deepseek.com"}
            is_native = any(h in provider.base_url for h in native_urls)
            if not is_native:
                kwargs["api_base"] = provider.base_url

        response = await litellm.acompletion(**kwargs)
        return response

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
            tokens_used = 0
            if hasattr(response, "usage") and response.usage:
                tokens_used = getattr(response.usage, "total_tokens", 0)

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

    async def handle_streaming_request(
        self, request: ChatCompletionRequest
    ) -> AsyncIterator[str]:
        """Full request handling for streaming — yields SSE lines."""
        candidates = await self._build_candidates(request.model)

        chain = FallbackChain(max_attempts=self.max_fallback_attempts)

        async def _call(
            provider: ProviderConfig, model_id: str
        ) -> Any:
            return await self.call_provider(provider, model_id, request)

        start_time = time.monotonic()
        response = await chain.execute(candidates, _call)
        winner = chain.successful_candidate

        completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())

        async for chunk in response:
            delta_content = None
            finish_reason = None

            if hasattr(chunk, "choices") and chunk.choices:
                choice = chunk.choices[0]
                delta = getattr(choice, "delta", None)
                if delta:
                    delta_content = getattr(delta, "content", None)
                finish_reason = getattr(choice, "finish_reason", None)

            sse_chunk = ChatCompletionChunk(
                id=completion_id,
                created=created,
                model=request.model,
                choices=[
                    StreamChoice(
                        index=0,
                        delta=ChatMessage(
                            role="assistant",
                            content=delta_content,
                        ),
                        finish_reason=finish_reason,
                    )
                ],
            )
            yield f"data: {sse_chunk.model_dump_json()}\n\n"

        yield "data: [DONE]\n\n"

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
        litellm_response: Any,
        requested_model: str,
        winner: ScoredCandidate | None,
    ) -> ChatCompletionResponse:
        """Convert litellm response to our schema."""
        choices: list[Choice] = []
        for c in litellm_response.choices:
            msg = c.message
            choices.append(
                Choice(
                    index=c.index,
                    message=ChatMessage(
                        role=msg.role,
                        content=getattr(msg, "content", None),
                        tool_calls=getattr(msg, "tool_calls", None),
                    ),
                    finish_reason=c.finish_reason,
                )
            )

        usage = None
        if litellm_response.usage:
            usage = Usage(
                prompt_tokens=litellm_response.usage.prompt_tokens,
                completion_tokens=litellm_response.usage.completion_tokens,
                total_tokens=litellm_response.usage.total_tokens,
            )

        # Use actual model_id from winner, not the requested name (e.g. "auto")
        actual_model = winner.model_id if winner else requested_model

        return ChatCompletionResponse(
            id=litellm_response.id,
            created=litellm_response.created,
            model=actual_model,
            choices=choices,
            usage=usage,
            provider=winner.provider_id if winner else None,
        )
