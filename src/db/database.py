"""Singleton async database manager using aiosqlite."""

from __future__ import annotations

import os
from pathlib import Path

import aiosqlite

from src.db.migrations import run_migrations

_db_connection: aiosqlite.Connection | None = None


def _resolve_db_path() -> Path:
    """Resolve database path from config or default."""
    # Allow env override, otherwise default
    db_path_str = os.environ.get("LLM_SWITCH_DB_PATH", "data/llm_switch.db")
    path = Path(db_path_str)
    if not path.is_absolute():
        # Relative to project root
        project_root = Path(__file__).resolve().parent.parent.parent
        path = project_root / path
    return path


async def get_db() -> aiosqlite.Connection:
    """Return the singleton database connection, creating it if needed."""
    global _db_connection
    if _db_connection is not None:
        return _db_connection

    db_path = _resolve_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    _db_connection = await aiosqlite.connect(str(db_path))
    _db_connection.row_factory = aiosqlite.Row
    await _db_connection.execute("PRAGMA journal_mode=WAL")
    await _db_connection.execute("PRAGMA foreign_keys=ON")
    return _db_connection


async def init_db() -> aiosqlite.Connection:
    """Initialize the database: get connection and run migrations."""
    db = await get_db()
    await run_migrations(db)
    return db


async def close_db() -> None:
    """Close the database connection."""
    global _db_connection
    if _db_connection is not None:
        await _db_connection.close()
        _db_connection = None
