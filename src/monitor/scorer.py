"""Scorer — computes composite provider/model scores from health-check data."""

from __future__ import annotations

import logging
import time
from statistics import median
from typing import Any

import aiosqlite

from src.db.queries import (
    get_provider_score,
    get_recent_health_checks,
    upsert_provider_score,
)
from src.pool.provider import ProviderConfig

logger = logging.getLogger(__name__)

# Seconds before freshness score starts decaying.
_FRESHNESS_FULL_WINDOW = 600  # 10 minutes
# After this many seconds the freshness score bottoms out.
_FRESHNESS_DECAY_WINDOW = 3600  # 1 hour


def _normalize_latency(median_ms: float) -> float:
    """Map median latency to a 0-1 score.  Lower latency = higher score."""
    if median_ms <= 0:
        return 1.0
    if median_ms <= 200:
        return 1.0
    if median_ms >= 10_000:
        return 0.1
    return round(1.0 - 0.9 * (median_ms - 200) / (10_000 - 200), 4)


def _freshness_score(last_check_timestamp: str | None) -> float:
    """Return 1.0 if data is fresh, decaying toward 0.1 over time."""
    if last_check_timestamp is None:
        return 0.0
    try:
        from datetime import datetime, timezone
        # SQLite CURRENT_TIMESTAMP is naive UTC — parse and make aware
        ts = last_check_timestamp.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - dt).total_seconds()
    except (ValueError, AttributeError, TypeError):
        return 0.0

    if age <= _FRESHNESS_FULL_WINDOW:
        return 1.0
    if age >= _FRESHNESS_DECAY_WINDOW:
        return 0.1
    return round(
        1.0 - 0.9 * (age - _FRESHNESS_FULL_WINDOW) / (_FRESHNESS_DECAY_WINDOW - _FRESHNESS_FULL_WINDOW),
        4,
    )


class Scorer:
    """Computes and persists weighted composite scores."""

    def __init__(self, db: aiosqlite.Connection, settings: dict[str, Any]) -> None:
        self.db = db
        default_weights = {
            "success_rate": 0.20,
            "quality": 0.15,
            "latency": 0.45,
            "quota_remaining": 0.10,
            "freshness": 0.10,
        }
        scoring_cfg = settings.get("scoring", {})
        self.weights: dict[str, float] = scoring_cfg.get("weights", default_weights)
        self.latency_hard_cap_ms: float = scoring_cfg.get("latency_hard_cap_ms", 15000)

    async def compute_score(self, provider_id: str, model_id: str) -> float:
        """Compute the composite score for a single provider/model pair."""
        checks = await get_recent_health_checks(
            self.db, provider_id, model_id, limit=20
        )

        if not checks:
            await upsert_provider_score(
                self.db,
                provider_id=provider_id,
                model_id=model_id,
                composite_score=0.5,
            )
            return 0.5

        # --- 1. Success rate ---
        successes = sum(1 for c in checks if c["success"])
        success_rate = successes / len(checks)

        # --- 2. Latency score (from successful checks only) ---
        latencies = [c["latency_ms"] for c in checks if c["success"] and c["latency_ms"] is not None]
        latency_score = _normalize_latency(median(latencies)) if latencies else 0.5

        # --- 3. Quality score (latest non-null value, or default) ---
        quality_scores = [
            c["output_quality_score"]
            for c in checks
            if c.get("output_quality_score") is not None
        ]
        quality_score = quality_scores[0] if quality_scores else 0.5

        # --- 4. Quota remaining (from provider_scores if available) ---
        existing = await get_provider_score(self.db, provider_id, model_id)
        quota_remaining = (
            existing.get("quota_remaining_pct", 1.0) if existing else 1.0
        )

        # --- 5. Freshness ---
        latest_timestamp = checks[0].get("timestamp") if checks else None
        freshness = _freshness_score(latest_timestamp)

        # --- Latency hard cap: penalize extremely slow providers ---
        median_latency = median(latencies) if latencies else 0
        if median_latency > self.latency_hard_cap_ms:
            latency_score = 0.01
            logger.info(
                "latency hard cap hit: provider=%s model=%s median=%.0fms > %dms",
                provider_id, model_id, median_latency, self.latency_hard_cap_ms,
            )

        # --- Composite ---
        w = self.weights
        composite = (
            w.get("success_rate", 0.20) * success_rate
            + w.get("quality", 0.15) * quality_score
            + w.get("latency", 0.45) * latency_score
            + w.get("quota_remaining", 0.10) * quota_remaining
            + w.get("freshness", 0.10) * freshness
        )
        composite = round(min(max(composite, 0.0), 1.0), 4)

        await upsert_provider_score(
            self.db,
            provider_id=provider_id,
            model_id=model_id,
            composite_score=composite,
        )

        logger.debug(
            "score provider=%s model=%s composite=%.4f "
            "(sr=%.2f q=%.2f lat=%.2f quota=%.2f fresh=%.2f)",
            provider_id,
            model_id,
            composite,
            success_rate,
            quality_score,
            latency_score,
            quota_remaining,
            freshness,
        )
        return composite

    async def update_all_scores(self, providers: list[ProviderConfig] | None = None) -> dict[str, float]:
        """Recompute scores for all provider/model pairs."""
        if providers is None:
            return {}

        results: dict[str, float] = {}
        for prov in providers:
            for model in prov.models:
                score = await self.compute_score(prov.id, model.id)
                results[f"{prov.id}/{model.id}"] = score
        return results
