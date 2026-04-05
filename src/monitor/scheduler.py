"""MonitorScheduler — registers periodic monitoring jobs via APScheduler."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

if TYPE_CHECKING:
    from src.discovery.scanner import DiscoveryScanner
    from src.monitor.auto_manager import AutoManager
    from src.monitor.health_checker import HealthChecker
    from src.monitor.scorer import Scorer

logger = logging.getLogger(__name__)


class MonitorScheduler:
    """Wraps APScheduler and wires up all monitoring periodic tasks."""

    def __init__(
        self,
        health_checker: HealthChecker,
        scorer: Scorer,
        auto_manager: AutoManager,
        pool_manager: Any,
        settings: dict[str, Any],
        scanner: DiscoveryScanner | None = None,
    ) -> None:
        self.health_checker = health_checker
        self.scorer = scorer
        self.auto_manager = auto_manager
        self.pool_manager = pool_manager
        self.settings = settings
        self.scanner = scanner
        self.scheduler = AsyncIOScheduler()

        # Track last request time per provider/model for idle detection
        self._last_activity: dict[str, float] = {}

    def record_activity(self, provider_id: str, model_id: str) -> None:
        """Called by Router after each real request — updates activity timestamp."""
        self._last_activity[f"{provider_id}/{model_id}"] = time.time()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Register all periodic jobs and start the scheduler."""
        monitor_cfg = self.settings.get("monitor", {})

        score_interval = monitor_cfg.get("score_update_interval_seconds", 300)
        idle_check_interval = monitor_cfg.get("idle_check_interval_seconds", 1800)  # 30 min
        manage_interval = 300  # 5 min

        # Only ping providers that have been idle (no real traffic) for 30+ minutes
        self.scheduler.add_job(
            self._ping_idle_providers,
            trigger=IntervalTrigger(seconds=idle_check_interval),
            id="ping_idle",
            name="Ping idle providers",
            replace_existing=True,
        )

        self.scheduler.add_job(
            self._update_all_scores,
            trigger=IntervalTrigger(seconds=score_interval),
            id="update_all_scores",
            name="Recompute provider scores",
            replace_existing=True,
        )

        self.scheduler.add_job(
            self.auto_manager.check_and_manage,
            trigger=IntervalTrigger(seconds=manage_interval),
            id="check_and_manage",
            name="Auto-manage providers",
            replace_existing=True,
        )

        # Model sync — check each provider's /v1/models for new/removed models
        self.scheduler.add_job(
            self._sync_models,
            trigger=IntervalTrigger(seconds=21600),  # every 6 hours
            id="model_sync",
            name="Sync provider model lists",
            replace_existing=True,
        )

        # Discovery scan (every 6 hours by default)
        if self.scanner is not None:
            discovery_cfg = self.settings.get("discovery", {})
            scan_interval = discovery_cfg.get("scan_interval_seconds", 21600)
            self.scheduler.add_job(
                self.scanner.scan_all_sources,
                trigger=IntervalTrigger(seconds=scan_interval),
                id="discovery_scan",
                name="Scan for new free LLM APIs",
                replace_existing=True,
            )

        self.scheduler.start()
        logger.info(
            "MonitorScheduler started — idle_ping=%ds score=%ds manage=%ds%s",
            idle_check_interval,
            score_interval,
            manage_interval,
            f" discovery={scan_interval}s" if self.scanner else "",
        )

    def stop(self) -> None:
        """Shut down the scheduler gracefully."""
        self.scheduler.shutdown(wait=False)
        logger.info("MonitorScheduler stopped.")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _sync_models(self) -> None:
        from src.monitor.model_sync import sync_all_models
        results = await sync_all_models(self.pool_manager)
        if results:
            try:
                from src.db.database import get_db
                from src.db.queries import log_event
                db = await get_db()
                for pid, changes in results.items():
                    added = len(changes.get("added", []))
                    removed = len(changes.get("removed", []))
                    await log_event(db, "model_sync", f"{pid}: +{added} -{removed} models", provider=pid)
            except Exception:
                pass

    async def _update_all_scores(self) -> None:
        providers = self.pool_manager.get_enabled_providers()
        await self.scorer.update_all_scores(providers)

    async def _ping_idle_providers(self) -> None:
        """Only ping providers that haven't had real traffic in 30+ minutes."""
        now = time.time()
        idle_threshold = self.settings.get("monitor", {}).get("idle_threshold_seconds", 1800)

        for provider in self.pool_manager.get_enabled_providers():
            seed_models = [m for m in provider.models if m.source == "seed"]
            for model in seed_models:
                key = f"{provider.id}/{model.id}"
                last = self._last_activity.get(key, 0)
                if now - last > idle_threshold:
                    logger.debug("Pinging idle provider=%s model=%s", provider.id, model.id)
                    await self.health_checker.ping_check(provider, model)
