"""AutoManager — disables unhealthy providers and retries disabled ones."""

from __future__ import annotations

import logging
import time
from typing import Any

import aiosqlite

from src.db.queries import get_recent_health_checks, upsert_provider_score
from src.monitor.health_checker import HealthChecker
from src.pool.manager import PoolManager
from src.pool.provider import ModelConfig, ProviderConfig

logger = logging.getLogger(__name__)


class AutoManager:
    """Periodically evaluates provider health and auto-disables/re-enables."""

    def __init__(self, pool_manager: PoolManager, db: aiosqlite.Connection, settings: dict[str, Any]) -> None:
        self.pool = pool_manager
        self.db = db
        self.settings = settings

        monitor_cfg = settings.get("monitor", {})
        self.disable_threshold: float = monitor_cfg.get("auto_disable_threshold", 0.20)
        self.check_window: int = monitor_cfg.get("auto_disable_window", 20)
        self.consecutive_fail_threshold: int = monitor_cfg.get("consecutive_fail_threshold", 10)
        self.backoff_minutes: list[int] = monitor_cfg.get(
            "retry_backoff_minutes", [30, 60, 120, 240, 1440]
        )

        # provider_id -> consecutive retry count (used for backoff index)
        self._retry_counts: dict[str, int] = {}
        # provider_id -> epoch when next retry is allowed
        self._next_retry: dict[str, float] = {}

        # Lightweight health-checker used only for probing disabled providers.
        self._health_checker = HealthChecker(pool_manager, db, settings)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def check_and_manage(self) -> None:
        """Evaluate all providers: disable unhealthy, probe disabled."""
        # On first run (or after restart), schedule retries for any
        # providers that are disabled but not yet in the retry queue.
        # This ensures disabled providers are always re-probed even
        # after container restarts.
        self._ensure_disabled_queued()
        await self._evaluate_enabled()
        await self._retry_disabled()

    def _ensure_disabled_queued(self) -> None:
        """Queue disabled providers for retry if not already scheduled."""
        for provider in self.pool.get_all_providers():
            if not provider.enabled and provider.has_api_key:
                if provider.id not in self._next_retry:
                    logger.info(
                        "Queuing disabled provider=%s for retry probe.",
                        provider.id,
                    )
                    self._schedule_retry(provider.id)

    # ------------------------------------------------------------------
    # Evaluate enabled providers
    # ------------------------------------------------------------------

    async def _evaluate_enabled(self) -> None:
        for provider in list(self.pool.get_enabled_providers()):
            # Skip providers without API key — no point disabling them
            if not provider.has_api_key:
                continue

            # Only evaluate seed models — auto-discovered models may not
            # support the health-check prompt format and should not cause
            # the entire provider to be disabled.
            seed_models = [m for m in provider.models if m.source == "seed"]
            if not seed_models:
                continue

            should_disable = False
            disable_reason = ""

            for model in seed_models:
                checks = await get_recent_health_checks(
                    self.db, provider.id, model.id, limit=self.check_window
                )
                if not checks:
                    continue

                # --- Consecutive auth errors (API key revoked/invalid) ---
                consecutive_auth = 0
                for c in checks:
                    if c.get("error_type") == "auth":
                        consecutive_auth += 1
                    else:
                        break

                if consecutive_auth >= 5:
                    should_disable = True
                    disable_reason = f"5+ consecutive auth errors on {model.id}"
                    break

                # --- Consecutive failures of any type ---
                # Only disable if the most recent N checks are ALL failures.
                # This avoids disabling on intermittent rate limits or timeouts.
                consecutive_fail = 0
                for c in checks:
                    if not c["success"]:
                        consecutive_fail += 1
                    else:
                        break

                if consecutive_fail >= self.consecutive_fail_threshold:
                    should_disable = True
                    disable_reason = (
                        f"{consecutive_fail} consecutive failures on {model.id}"
                    )
                    break

            if should_disable:
                logger.warning(
                    "Disabling provider=%s: %s", provider.id, disable_reason,
                )
                await self.pool.disable_provider(provider.id, reason=disable_reason)
                self._schedule_retry(provider.id)

    # ------------------------------------------------------------------
    # Retry disabled providers
    # ------------------------------------------------------------------

    async def _retry_disabled(self) -> None:
        now = time.time()
        for provider_id, next_time in list(self._next_retry.items()):
            if now < next_time:
                continue

            provider = self.pool.get_provider(provider_id)
            if provider is None:
                self._next_retry.pop(provider_id, None)
                self._retry_counts.pop(provider_id, None)
                continue

            if provider.enabled:
                self._next_retry.pop(provider_id, None)
                self._retry_counts.pop(provider_id, None)
                continue

            if not provider.models:
                continue
            # Prefer a seed model for probing
            seed = [m for m in provider.models if m.source == "seed"]
            model = seed[0] if seed else provider.models[0]

            result = await self._health_checker.ping_check(provider, model)
            if result["success"]:
                logger.info(
                    "Probe succeeded for provider=%s — re-enabling with reduced score.",
                    provider_id,
                )
                await self.pool.enable_provider(provider_id)
                await upsert_provider_score(
                    self.db,
                    provider_id=provider_id,
                    model_id=model.id,
                    composite_score=0.4,
                )
                self._next_retry.pop(provider_id, None)
                self._retry_counts.pop(provider_id, None)
            else:
                count = self._retry_counts.get(provider_id, 0) + 1
                self._retry_counts[provider_id] = count
                self._schedule_retry(provider_id)
                logger.info(
                    "Probe failed for provider=%s — retry #%d scheduled.",
                    provider_id,
                    count,
                )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _schedule_retry(self, provider_id: str) -> None:
        count = self._retry_counts.get(provider_id, 0)
        idx = min(count, len(self.backoff_minutes) - 1)
        delay_seconds = self.backoff_minutes[idx] * 60
        self._next_retry[provider_id] = time.time() + delay_seconds
        logger.debug(
            "Retry for provider=%s scheduled in %d minutes (attempt %d).",
            provider_id,
            self.backoff_minutes[idx],
            count + 1,
        )
