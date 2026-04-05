"""DiscoveryDiffer — filters discovered entries against the known provider pool."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

if TYPE_CHECKING:
    from src.pool.manager import PoolManager


def _normalise_url(url: str) -> str:
    """Strip scheme, trailing slashes, and common path prefixes for comparison."""
    parsed = urlparse(url.strip().rstrip("/"))
    host = (parsed.netloc or parsed.path).lower()
    # Remove www. prefix.
    host = re.sub(r"^www\.", "", host)
    return host


def _normalise_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


class DiscoveryDiffer:
    """Compares discovered API entries against the existing pool."""

    def __init__(self, pool_manager: PoolManager) -> None:
        self.pool = pool_manager

    def diff(
        self, discovered: list[dict[str, Any]], source_name: str
    ) -> list[dict[str, Any]]:
        """Return only entries that are *not* already in the pool.

        Each returned dict has an extra ``"source"`` field set to *source_name*.
        """
        # Build lookup sets from the current pool.
        known_urls: set[str] = set()
        known_names: set[str] = set()

        all_providers = self.pool.get_enabled_providers()
        # Also include disabled providers so we don't re-discover them.
        for pid in list(getattr(self.pool, "_providers", {}).keys()):
            prov = self.pool.get_provider(pid)
            if prov is not None:
                all_providers_extended = list({id(p): p for p in [*all_providers, prov]}.values())
        else:
            all_providers_extended = list(all_providers)

        for prov in all_providers_extended:
            if prov.base_url:
                known_urls.add(_normalise_url(prov.base_url))
            known_names.add(_normalise_name(prov.name))

        new_entries: list[dict[str, Any]] = []
        for entry in discovered:
            # Extract URL from the entry (may be a plain string or a link dict).
            url_raw = entry.get("base_url") or entry.get("api_endpoint") or entry.get("url", "")
            if isinstance(url_raw, dict):
                url_raw = url_raw.get("url", "")
            url_norm = _normalise_url(url_raw) if url_raw else ""

            name_raw = entry.get("name") or entry.get("provider") or entry.get("Provider") or ""
            if isinstance(name_raw, dict):
                name_raw = name_raw.get("text", "")
            name_norm = _normalise_name(name_raw)

            # Skip if URL or name matches a known provider.
            if url_norm and url_norm in known_urls:
                continue
            if name_norm and name_norm in known_names:
                continue

            entry["source"] = source_name
            new_entries.append(entry)

        return new_entries
