"""Load and save YAML configuration files."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

from src.pool.provider import ProvidersFile


def load_providers(path: str | Path) -> ProvidersFile:
    """Load providers from a YAML file. Returns empty ProvidersFile if not found."""
    p = Path(path)
    if not p.exists():
        return ProvidersFile()
    with p.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not data:
        return ProvidersFile()
    return ProvidersFile.model_validate(data)


def save_providers(path: str | Path, providers_file: ProvidersFile) -> None:
    """Save providers to a YAML file."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = providers_file.model_dump(mode="json")
    with p.open("w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)


def load_settings(path: str | Path) -> dict[str, Any]:
    """Load settings.yaml. Returns empty dict if not found."""
    p = Path(path)
    if not p.exists():
        return {}
    with p.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data or {}


def load_model_aliases(path: str | Path) -> dict[str, Any]:
    """Load model_aliases.yaml. Returns the aliases dict if not found."""
    p = Path(path)
    if not p.exists():
        return {}
    with p.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not data:
        return {}
    return data.get("aliases", data)


def load_discovery_sources(path: str | Path) -> dict[str, Any]:
    """Load discovery_sources.yaml. Returns empty dict if not found."""
    p = Path(path)
    if not p.exists():
        return {}
    with p.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data or {}


# ---------------------------------------------------------------------------
# API Keys
# ---------------------------------------------------------------------------


def load_api_keys(path: str | Path) -> dict[str, str]:
    """Load api_keys.yaml and inject into os.environ. Returns the loaded keys."""
    p = Path(path)
    if not p.exists():
        return {}
    with p.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not data or not isinstance(data, dict):
        return {}
    count = 0
    for env_name, value in data.items():
        if value:
            os.environ[env_name] = str(value)
            count += 1
    logger.info("Loaded %d API keys from api_keys.yaml", count)
    return data


def save_api_key(path: str | Path, env_name: str, value: str) -> None:
    """Save a single API key to api_keys.yaml and set os.environ."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    # Load existing
    existing: dict[str, str] = {}
    if p.exists():
        with p.open("r", encoding="utf-8") as f:
            existing = yaml.safe_load(f) or {}

    # Update
    existing[env_name] = value
    os.environ[env_name] = value

    # Save
    with p.open("w", encoding="utf-8") as f:
        yaml.dump(existing, f, default_flow_style=False, sort_keys=False)

    logger.info("Saved API key %s", env_name)
