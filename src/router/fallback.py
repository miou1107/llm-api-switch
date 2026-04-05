"""Fallback chain for trying multiple provider candidates in order."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

from src.router.strategies import ScoredCandidate

logger = logging.getLogger(__name__)


@dataclass
class AttemptResult:
    candidate: ScoredCandidate
    success: bool
    response: Any = None
    error: Exception | None = None


class FallbackChain:
    """Try candidates in score-descending order until one succeeds."""

    def __init__(self, max_attempts: int = 3) -> None:
        self.max_attempts = max_attempts
        self.attempts: list[AttemptResult] = []

    async def execute(
        self,
        candidates: list[ScoredCandidate],
        call_fn: Callable[..., Awaitable[Any]],
    ) -> Any:
        """Try candidates in order until one succeeds.

        Args:
            candidates: Scored candidates sorted by preference (best first).
            call_fn: Async callable(provider_config, model_id, request)
                     that returns a response or raises on failure.

        Returns:
            The successful response.

        Raises:
            RuntimeError: If all attempts fail.
        """
        self.attempts = []
        errors: list[str] = []

        to_try = candidates[: self.max_attempts]

        for candidate in to_try:
            try:
                logger.info(
                    "Attempting provider=%s model=%s (score=%.3f)",
                    candidate.provider_id,
                    candidate.model_id,
                    candidate.composite_score,
                )
                response = await call_fn(
                    candidate.provider_config,
                    candidate.model_id,
                )
                result = AttemptResult(
                    candidate=candidate, success=True, response=response
                )
                self.attempts.append(result)
                logger.info(
                    "Success: provider=%s model=%s",
                    candidate.provider_id,
                    candidate.model_id,
                )
                return response

            except Exception as exc:
                error_msg = f"{candidate.provider_id}/{candidate.model_id}: {exc}"
                errors.append(error_msg)
                result = AttemptResult(
                    candidate=candidate, success=False, error=exc
                )
                self.attempts.append(result)
                logger.warning("Failed: %s", error_msg)

        raise RuntimeError(
            f"All {len(to_try)} provider attempts failed: "
            + "; ".join(errors)
        )

    @property
    def successful_candidate(self) -> ScoredCandidate | None:
        """Return the candidate that succeeded, if any."""
        for attempt in self.attempts:
            if attempt.success:
                return attempt.candidate
        return None

    @property
    def failed_candidates(self) -> list[ScoredCandidate]:
        """Return all candidates that failed."""
        return [a.candidate for a in self.attempts if not a.success]
