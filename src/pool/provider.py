"""Pydantic v2 models for provider and model configuration."""

from __future__ import annotations

from pydantic import BaseModel


class RateLimits(BaseModel):
    rpm: int = 30
    rpd: int = 14400
    tpm: int = 100000


class ModelConfig(BaseModel):
    id: str
    context_window: int = 8192
    max_output_tokens: int = 8192
    supports_streaming: bool = True
    supports_function_calling: bool = False
    rate_limits: RateLimits = RateLimits()
    source: str = "seed"  # seed | auto-discovered


class ProviderConfig(BaseModel):
    id: str
    name: str
    base_url: str
    api_key_env: str | None = None
    litellm_provider: str | None = None
    source: str = "manual"  # seed | manual | auto-discovered
    discovered_from: str | None = None
    discovered_at: str | None = None
    enabled: bool = True
    disable_reason: str | None = None
    models: list[ModelConfig] = []

    @property
    def api_key(self) -> str | None:
        """Get the next API key (round-robin if multiple)."""
        if not self.api_key_env:
            return None
        from src.pool.key_store import get_key
        return get_key(self.api_key_env)

    @property
    def api_key_count(self) -> int:
        """How many keys are configured for this provider."""
        if not self.api_key_env:
            return 0
        from src.pool.key_store import get_key_count
        return get_key_count(self.api_key_env)

    @property
    def has_api_key(self) -> bool:
        return self.api_key_count > 0


class ProvidersFile(BaseModel):
    providers: list[ProviderConfig] = []
