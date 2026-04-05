"""DiscoveryValidator — probes a discovered API to check if it actually works."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Minimal OpenAI-compatible chat completion payload.
_PROBE_PAYLOAD = {
    "model": "gpt-3.5-turbo",  # placeholder; many free endpoints accept any model name
    "messages": [{"role": "user", "content": "Say hi"}],
    "max_tokens": 5,
}


class DiscoveryValidator:
    """Sends a lightweight probe to a discovered endpoint."""

    def __init__(self, settings: dict) -> None:
        self.timeout = settings.get("discovery", {}).get("probe_timeout_seconds", 30)

    async def validate(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Probe a discovered API and enrich *entry* with validation results.

        Added fields:
        - ``validated`` (bool)
        - ``validation_error`` (str | None)
        - ``latency_ms`` (float | None)
        """
        url = self._resolve_url(entry)
        if not url:
            entry.update(
                validated=False,
                validation_error="no usable URL found in entry",
                latency_ms=None,
            )
            return entry

        # Ensure the URL points to the chat/completions endpoint.
        endpoint = url.rstrip("/")
        if not endpoint.endswith("/chat/completions"):
            endpoint = f"{endpoint}/chat/completions"

        api_key = entry.get("api_key") or entry.get("key") or ""
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    endpoint, json=_PROBE_PAYLOAD, headers=headers
                )
            latency_ms = round((time.monotonic() - start) * 1000, 1)

            if resp.status_code == 200:
                entry.update(
                    validated=True,
                    validation_error=None,
                    latency_ms=latency_ms,
                )
            else:
                entry.update(
                    validated=False,
                    validation_error=f"HTTP {resp.status_code}: {resp.text[:300]}",
                    latency_ms=latency_ms,
                )
        except httpx.TimeoutException:
            entry.update(
                validated=False,
                validation_error="timeout",
                latency_ms=round((time.monotonic() - start) * 1000, 1),
            )
        except Exception as exc:  # noqa: BLE001
            entry.update(
                validated=False,
                validation_error=str(exc)[:500],
                latency_ms=None,
            )
            logger.warning("Validation failed for %s: %s", url, exc)

        return entry

    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_url(entry: dict[str, Any]) -> str:
        """Best-effort extraction of a base URL from the entry dict."""
        for key in ("base_url", "api_endpoint", "url", "URL", "Endpoint"):
            val = entry.get(key)
            if not val:
                continue
            if isinstance(val, dict):
                val = val.get("url", "")
            val = str(val).strip()
            if val.startswith("http"):
                return val
        return ""
