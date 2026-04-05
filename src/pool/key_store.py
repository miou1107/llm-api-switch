"""API Key store — supports multiple keys per provider with round-robin rotation."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# Global key store: env_name -> list of keys
_key_store: dict[str, list[str]] = {}

# Round-robin counters: env_name -> index
_key_counters: dict[str, int] = {}


def load_keys(path: str | Path) -> dict[str, list[str]]:
    """Load api_keys.yaml into the global key store.

    Supports both single string and list values:
        GROQ_API_KEY: "gsk_abc"
        GEMINI_API_KEY:
          - "AIza_1"
          - "AIza_2"
    """
    global _key_store
    p = Path(path)
    if not p.exists():
        return {}
    with p.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not data or not isinstance(data, dict):
        return {}

    _key_store = {}
    total = 0
    for env_name, value in data.items():
        if isinstance(value, list):
            keys = [str(v) for v in value if v]
        elif value:
            keys = [str(value)]
        else:
            keys = []
        if keys:
            _key_store[env_name] = keys
            total += len(keys)

    logger.info("Loaded %d API keys for %d providers", total, len(_key_store))
    return _key_store


def load_keys_from_dict(data: dict[str, str]) -> None:
    """Load API keys from a dictionary (e.g., from database).

    Merges with existing keys, giving priority to already-loaded keys.
    Format: {env_name: key_value}
    """
    global _key_store
    total_added = 0
    for env_name, key_value in data.items():
        if not key_value:
            continue
        if env_name not in _key_store:
            _key_store[env_name] = [key_value]
            total_added += 1
        elif key_value not in _key_store[env_name]:
            _key_store[env_name].append(key_value)
            total_added += 1

    if total_added > 0:
        logger.info("Loaded %d additional API keys from database", total_added)


def get_key(env_name: str) -> str | None:
    """Get the next API key for a provider (round-robin if multiple)."""
    keys = _key_store.get(env_name, [])
    if not keys:
        return None
    if len(keys) == 1:
        return keys[0]
    # Round-robin
    idx = _key_counters.get(env_name, 0)
    key = keys[idx % len(keys)]
    _key_counters[env_name] = idx + 1
    return key


def get_all_keys(env_name: str) -> list[str]:
    """Get all keys for a provider."""
    return list(_key_store.get(env_name, []))


def get_key_count(env_name: str) -> int:
    """Get number of keys for a provider."""
    return len(_key_store.get(env_name, []))


def add_key(path: str | Path, env_name: str, value: str) -> None:
    """Add a key to the store and persist to yaml."""
    global _key_store
    if env_name not in _key_store:
        _key_store[env_name] = []
    # Don't add duplicates
    if value not in _key_store[env_name]:
        _key_store[env_name].append(value)
    _save(path)
    logger.info("Added key for %s (now %d keys)", env_name, len(_key_store[env_name]))


def remove_key(path: str | Path, env_name: str, index: int) -> bool:
    """Remove a key by index. Returns True if removed."""
    global _key_store
    keys = _key_store.get(env_name, [])
    if 0 <= index < len(keys):
        keys.pop(index)
        if not keys:
            _key_store.pop(env_name, None)
        _save(path)
        logger.info("Removed key #%d for %s", index, env_name)
        return True
    return False


def _save(path: str | Path) -> None:
    """Persist key store to yaml."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Flatten single-key lists to plain strings for cleaner yaml
    out: dict[str, Any] = {}
    for env_name, keys in _key_store.items():
        if len(keys) == 1:
            out[env_name] = keys[0]
        else:
            out[env_name] = keys
    with p.open("w", encoding="utf-8") as f:
        yaml.dump(out, f, default_flow_style=False, sort_keys=False)
