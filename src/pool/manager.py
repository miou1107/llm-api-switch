"""Pool manager — loads providers, resolves models, manages the provider pool."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import aiosqlite

from src.pool.config_loader import load_model_aliases, load_providers, save_providers
from src.pool.provider import ModelConfig, ProviderConfig, ProvidersFile

logger = logging.getLogger(__name__)


class PoolManager:
    """Manages the pool of LLM providers and model resolution."""

    def __init__(self, config_dir: str | Path, db: aiosqlite.Connection) -> None:
        self._config_dir = Path(config_dir)
        self._db = db
        self._providers_file = ProvidersFile()
        self._aliases: dict[str, list[dict[str, str]]] = {}
        self._providers_path: Path | None = None

    async def initialize(self) -> None:
        """Load providers from YAML (providers.yaml first, fall back to seed) and aliases."""
        providers_path = self._config_dir / "providers.yaml"
        seed_path = self._config_dir.parent / "data" / "seed_providers.yaml"

        if providers_path.exists():
            self._providers_file = load_providers(providers_path)
            self._providers_path = providers_path
            logger.info("Loaded %d providers from providers.yaml", len(self._providers_file.providers))
        elif seed_path.exists():
            self._providers_file = load_providers(seed_path)
            self._providers_path = providers_path  # future saves go to config/
            logger.info("Loaded %d providers from seed_providers.yaml", len(self._providers_file.providers))
        else:
            self._providers_file = ProvidersFile()
            self._providers_path = providers_path

        aliases_path = self._config_dir / "model_aliases.yaml"
        self._aliases = load_model_aliases(aliases_path)
        logger.info("Loaded %d model aliases", len(self._aliases))

    @property
    def providers(self) -> list[ProviderConfig]:
        """All loaded providers."""
        return self._providers_file.providers

    @property
    def aliases(self) -> dict[str, list[dict[str, str]]]:
        """All loaded model aliases."""
        return self._aliases

    # -------------------------------------------------------------------
    # Provider accessors
    # -------------------------------------------------------------------

    def get_all_providers(self) -> list[ProviderConfig]:
        """Return all providers."""
        return list(self._providers_file.providers)

    def get_enabled_providers(self) -> list[ProviderConfig]:
        """Return only enabled providers."""
        return [p for p in self._providers_file.providers if p.enabled]

    def get_provider(self, provider_id: str) -> ProviderConfig | None:
        """Return a single provider by id, or None."""
        for p in self._providers_file.providers:
            if p.id == provider_id:
                return p
        return None

    # -------------------------------------------------------------------
    # Model resolution
    # -------------------------------------------------------------------

    def get_models_for_alias(self, alias_name: str) -> list[tuple[ProviderConfig, ModelConfig]]:
        """Resolve an alias to a list of (provider, model) tuples."""
        entries = self._aliases.get(alias_name, [])
        results: list[tuple[ProviderConfig, ModelConfig]] = []
        for entry in entries:
            provider = self.get_provider(entry["provider"])
            if provider is None or not provider.enabled:
                continue
            for model in provider.models:
                if model.id == entry["model"]:
                    results.append((provider, model))
                    break
        return results

    def resolve_model(self, model_name: str) -> list[tuple[ProviderConfig, ModelConfig]]:
        """Resolve a model name to (provider, model) pairs.

        Checks aliases first, then does a direct match across all enabled providers.
        """
        # Check aliases
        alias_results = self.get_models_for_alias(model_name)
        if alias_results:
            return alias_results

        # Direct match across enabled providers
        results: list[tuple[ProviderConfig, ModelConfig]] = []
        for provider in self.get_enabled_providers():
            for model in provider.models:
                if model.id == model_name:
                    results.append((provider, model))
        return results

    def get_all_available_models(self) -> list[dict[str, Any]]:
        """Return a flat list of model info dicts (suitable for /v1/models)."""
        models: list[dict[str, Any]] = []
        seen: set[str] = set()

        # Aliases first
        for alias_name in self._aliases:
            if alias_name not in seen:
                seen.add(alias_name)
                models.append({
                    "id": alias_name,
                    "object": "model",
                    "type": "alias",
                    "providers": [
                        e["provider"] for e in self._aliases[alias_name]
                    ],
                })

        # Direct models from enabled providers
        for provider in self.get_enabled_providers():
            for model in provider.models:
                model_id = model.id
                if model_id not in seen:
                    seen.add(model_id)
                    models.append({
                        "id": model_id,
                        "object": "model",
                        "type": "direct",
                        "provider": provider.id,
                        "context_window": model.context_window,
                        "max_output_tokens": model.max_output_tokens,
                        "supports_streaming": model.supports_streaming,
                        "supports_function_calling": model.supports_function_calling,
                    })
        return models

    # -------------------------------------------------------------------
    # Provider mutations
    # -------------------------------------------------------------------

    async def add_provider(self, config: ProviderConfig) -> None:
        """Add a new provider and persist to YAML."""
        self._providers_file.providers.append(config)
        await self._save()

    async def disable_provider(self, provider_id: str, reason: str | None = None) -> bool:
        """Disable a provider. Returns True if found."""
        provider = self.get_provider(provider_id)
        if provider is None:
            return False
        provider.enabled = False
        provider.disable_reason = reason
        await self._save()
        return True

    async def enable_provider(self, provider_id: str) -> bool:
        """Enable a provider. Returns True if found."""
        provider = self.get_provider(provider_id)
        if provider is None:
            return False
        provider.enabled = True
        provider.disable_reason = None
        await self._save()
        return True

    async def _save(self) -> None:
        """Persist current providers to YAML."""
        if self._providers_path:
            save_providers(self._providers_path, self._providers_file)
