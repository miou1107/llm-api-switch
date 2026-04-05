"""Quota tracker — sliding-window rate-limit checks against the database."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import aiosqlite

from src.db.queries import get_quota_usage_since, record_quota_usage
from src.pool.provider import RateLimits


class QuotaTracker:
    """Track and check API quota usage via sliding windows."""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def record_usage(
        self,
        provider_id: str,
        model_id: str,
        tokens: int,
    ) -> None:
        """Record a single request's token usage."""
        await record_quota_usage(self._db, provider_id, model_id, tokens)

    async def check_quota(
        self,
        provider_id: str,
        model_id: str,
        rate_limits: RateLimits,
    ) -> float:
        """Check remaining quota as a fraction 0.0–1.0.

        Checks both per-minute (rpm/tpm) and per-day (rpd) windows,
        returns the minimum remaining fraction.
        """
        now = datetime.now(timezone.utc)

        # Per-minute window
        minute_ago = (now - timedelta(minutes=1)).isoformat()
        minute_usage = await get_quota_usage_since(
            self._db, provider_id, model_id, minute_ago,
        )
        rpm_remaining = max(0.0, 1.0 - minute_usage["total_requests"] / rate_limits.rpm)
        tpm_remaining = max(0.0, 1.0 - minute_usage["total_tokens"] / rate_limits.tpm)

        # Per-day window
        day_ago = (now - timedelta(days=1)).isoformat()
        day_usage = await get_quota_usage_since(
            self._db, provider_id, model_id, day_ago,
        )
        rpd_remaining = max(0.0, 1.0 - day_usage["total_requests"] / rate_limits.rpd)

        return min(rpm_remaining, tpm_remaining, rpd_remaining)
