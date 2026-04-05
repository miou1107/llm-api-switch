"""Routing strategy functions for provider/model selection."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ScoredCandidate:
    provider_id: str
    model_id: str
    provider_config: Any  # ProviderConfig (avoid circular import)
    model_config: Any  # ModelConfig
    composite_score: float = 0.0


async def best_score(candidates: list[ScoredCandidate], **_kwargs: Any) -> ScoredCandidate:
    """Pick the candidate with the highest composite_score."""
    if not candidates:
        raise ValueError("No candidates available")
    return max(candidates, key=lambda c: c.composite_score)


async def weighted_random(candidates: list[ScoredCandidate], **_kwargs: Any) -> ScoredCandidate:
    """Random pick weighted by composite_score.

    Candidates with higher scores are more likely to be selected.
    Scores are shifted so the minimum becomes a small positive value
    to ensure all candidates have a non-zero chance.
    """
    if not candidates:
        raise ValueError("No candidates available")
    if len(candidates) == 1:
        return candidates[0]

    scores = [c.composite_score for c in candidates]
    min_score = min(scores)
    # Shift scores so all are positive; add small epsilon to avoid zero weights
    weights = [s - min_score + 0.01 for s in scores]
    return random.choices(candidates, weights=weights, k=1)[0]


_round_robin_counters: dict[str, int] = {}


async def round_robin(
    candidates: list[ScoredCandidate],
    counter_key: str = "default",
    **_kwargs: Any,
) -> ScoredCandidate:
    """Simple round robin across candidates.

    Uses a module-level counter dict keyed by counter_key to persist
    state across calls within the same process.
    """
    if not candidates:
        raise ValueError("No candidates available")

    idx = _round_robin_counters.get(counter_key, 0)
    selected = candidates[idx % len(candidates)]
    _round_robin_counters[counter_key] = idx + 1
    return selected


STRATEGIES: dict[str, Any] = {
    "best_score": best_score,
    "weighted_random": weighted_random,
    "round_robin": round_robin,
}
