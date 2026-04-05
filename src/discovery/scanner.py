"""DiscoveryScanner — fetches discovery sources, parses, diffs, and optionally auto-approves."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import aiosqlite
import httpx
import yaml

from src.db.queries import record_discovery
from src.discovery.differ import DiscoveryDiffer
from src.discovery.parsers.markdown_table import parse_markdown_tables
from src.discovery.validator import DiscoveryValidator
from src.pool.manager import PoolManager
from src.pool.provider import ModelConfig, ProviderConfig, RateLimits

logger = logging.getLogger(__name__)

# Map source type -> parser coroutine.
_PARSERS: dict[str, Any] = {
    "markdown_table": parse_markdown_tables,
}


class DiscoveryScanner:
    """Scans configured sources for new free LLM APIs."""

    def __init__(
        self,
        pool_manager: PoolManager,
        db: aiosqlite.Connection,
        settings: dict[str, Any],
        config_dir: str | Path,
    ) -> None:
        self.pool = pool_manager
        self.db = db
        self.settings = settings
        self.config_dir = Path(config_dir)
        self.differ = DiscoveryDiffer(pool_manager)
        self.validator = DiscoveryValidator(settings)

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    async def scan_all_sources(self) -> list[dict[str, Any]]:
        """Scan every source defined in ``discovery_sources.yaml``."""
        sources = self._load_sources()
        all_new: list[dict[str, Any]] = []
        for source in sources:
            try:
                new_entries = await self.scan_source(source)
                all_new.extend(new_entries)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Failed to scan source=%s: %s", source.get("name"), exc
                )
        logger.info("Discovery scan complete — %d new entries found.", len(all_new))
        return all_new

    async def scan_source(self, source: dict[str, Any]) -> list[dict[str, Any]]:
        """Fetch, parse, diff, and handle a single discovery source."""
        name = source.get("name", "unknown")
        url = source["url"]
        src_type = source.get("type", "markdown_table")

        logger.info("Scanning source=%s url=%s", name, url)

        # 1. Fetch raw content.
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        content = resp.text

        # 2. Parse.
        parser = _PARSERS.get(src_type)
        if parser is None:
            logger.warning("No parser for type=%s, skipping source=%s", src_type, name)
            return []
        parsed = await parser(content)

        # 3. Diff against known pool.
        new_entries = self.differ.diff(parsed, source_name=name)
        if not new_entries:
            logger.info("No new entries from source=%s", name)
            return []

        # 4. Validate and auto-add each new entry.
        for entry in new_entries:
            provider_name = entry.get("name") or entry.get("provider") or None
            base_url = entry.get("base_url") or entry.get("url") or None

            validated = await self.validator.validate(entry)
            if validated.get("validated"):
                try:
                    provider = self._entry_to_provider(validated)
                    await self.pool.add_provider(provider)
                    await record_discovery(
                        self.db, source_name=name, provider_name=provider_name,
                        base_url=base_url, raw_data=json.dumps(entry),
                        parsed_data=None, status="auto_added",
                    )
                    logger.info("Auto-added provider=%s from source=%s", provider.id, name)
                except Exception as exc:
                    logger.error("Failed to add discovered provider: %s", exc)
                    await record_discovery(
                        self.db, source_name=name, provider_name=provider_name,
                        base_url=base_url, raw_data=json.dumps(entry),
                        parsed_data=None, status="error",
                    )
            else:
                await record_discovery(
                    self.db, source_name=name, provider_name=provider_name,
                    base_url=base_url, raw_data=json.dumps(entry),
                    parsed_data=None, status="rejected",
                )
                logger.debug("Rejected entry from %s: %s", name, validated.get("validation_error"))

        return new_entries

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _load_sources(self) -> list[dict[str, Any]]:
        path = self.config_dir / "discovery_sources.yaml"
        if not path.exists():
            logger.warning("discovery_sources.yaml not found at %s", path)
            return []
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        return data.get("sources", [])

    @staticmethod
    def _entry_to_provider(entry: dict[str, Any]) -> ProviderConfig:
        """Best-effort conversion of a raw discovery entry into a ProviderConfig."""
        name_raw = (
            entry.get("name")
            or entry.get("provider")
            or entry.get("Provider")
            or "discovered"
        )
        if isinstance(name_raw, dict):
            name_raw = name_raw.get("text", "discovered")

        provider_id = (
            name_raw.lower()
            .replace(" ", "-")
            .replace("/", "-")
            .replace(".", "-")
        )[:40]

        url_raw = (
            entry.get("base_url")
            or entry.get("api_endpoint")
            or entry.get("url")
            or ""
        )
        if isinstance(url_raw, dict):
            url_raw = url_raw.get("url", "")

        model_name = entry.get("model") or entry.get("Model") or "default"
        if isinstance(model_name, dict):
            model_name = model_name.get("text", "default")

        default_model = ModelConfig(
            id=str(model_name),
            context_window=4096,
            max_output_tokens=4096,
            supports_streaming=True,
            supports_function_calling=False,
            rate_limits=RateLimits(rpm=10, rpd=1000, tpm=60000),
        )

        return ProviderConfig(
            id=provider_id,
            name=str(name_raw),
            base_url=str(url_raw).rstrip("/"),
            api_key_env=entry.get("api_key_env"),
            litellm_provider="openai",
            source="auto-discovered",
            enabled=True,
            models=[default_model],
        )
