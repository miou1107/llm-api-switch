"""Application entrypoint for llm-api-switch."""

from __future__ import annotations

import logging

from src.gateway.app import create_app
from src.pool.config_loader import load_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# Module-level app for `uvicorn src.main:app`
app = create_app()


if __name__ == "__main__":
    import uvicorn
    from pathlib import Path

    config_dir = Path(__file__).resolve().parent.parent / "config"
    settings = load_settings(config_dir / "settings.yaml")
    gateway_cfg = settings.get("gateway", {})
    host = gateway_cfg.get("host", "0.0.0.0")
    port = gateway_cfg.get("port", 8000)

    uvicorn.run(
        "src.main:app",
        host=host,
        port=port,
        reload=False,
        log_level="info",
    )
