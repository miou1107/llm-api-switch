"""Async query functions for health checks, quotas, scores, discoveries, and API keys."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime
from typing import Any

import aiosqlite


def _row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
    """Convert an aiosqlite.Row to a plain dict."""
    return dict(row)


# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------

async def record_health_check(
    db: aiosqlite.Connection,
    provider_id: str,
    model_id: str,
    latency_ms: float,
    success: bool,
    error_type: str | None = None,
    quality_score: float | None = None,
    tokens_used: int | None = None,
) -> int:
    """Insert a health-check record. Returns the new row id."""
    cursor = await db.execute(
        """
        INSERT INTO health_checks
            (provider_id, model_id, latency_ms, success, error_type,
             output_quality_score, tokens_used)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (provider_id, model_id, latency_ms, success, error_type,
         quality_score, tokens_used),
    )
    await db.commit()
    return cursor.lastrowid  # type: ignore[return-value]


async def get_recent_health_checks(
    db: aiosqlite.Connection,
    provider_id: str,
    model_id: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return the most recent health checks for a provider/model pair."""
    cursor = await db.execute(
        """
        SELECT * FROM health_checks
        WHERE provider_id = ? AND model_id = ?
        ORDER BY timestamp DESC
        LIMIT ?
        """,
        (provider_id, model_id, limit),
    )
    rows = await cursor.fetchall()
    return [_row_to_dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Quota usage
# ---------------------------------------------------------------------------

async def record_quota_usage(
    db: aiosqlite.Connection,
    provider_id: str,
    model_id: str,
    tokens_consumed: int,
    window_type: str = "minute",
) -> int:
    """Insert a quota-usage record. Returns the new row id."""
    cursor = await db.execute(
        """
        INSERT INTO quota_usage
            (provider_id, model_id, tokens_consumed, window_type)
        VALUES (?, ?, ?, ?)
        """,
        (provider_id, model_id, tokens_consumed, window_type),
    )
    await db.commit()
    return cursor.lastrowid  # type: ignore[return-value]


async def get_quota_usage_since(
    db: aiosqlite.Connection,
    provider_id: str,
    model_id: str,
    since_timestamp: str | datetime,
) -> dict[str, int]:
    """Return total requests and tokens since a given timestamp.

    Returns:
        {"total_requests": int, "total_tokens": int}
    """
    if isinstance(since_timestamp, datetime):
        since_timestamp = since_timestamp.isoformat()

    cursor = await db.execute(
        """
        SELECT
            COALESCE(SUM(requests_count), 0) AS total_requests,
            COALESCE(SUM(tokens_consumed), 0) AS total_tokens
        FROM quota_usage
        WHERE provider_id = ? AND model_id = ? AND timestamp >= ?
        """,
        (provider_id, model_id, since_timestamp),
    )
    row = await cursor.fetchone()
    return {
        "total_requests": row["total_requests"],  # type: ignore[index]
        "total_tokens": row["total_tokens"],  # type: ignore[index]
    }


# ---------------------------------------------------------------------------
# Provider scores
# ---------------------------------------------------------------------------

async def upsert_provider_score(
    db: aiosqlite.Connection,
    provider_id: str,
    model_id: str,
    **scores: float,
) -> None:
    """Insert or update a provider score row.

    Accepted keyword args: latency_p50_ms, success_rate, quality_score,
    quota_remaining_pct, composite_score.
    """
    allowed = {
        "latency_p50_ms", "success_rate", "quality_score",
        "quota_remaining_pct", "composite_score",
    }
    filtered = {k: v for k, v in scores.items() if k in allowed}

    # Build SET clause for the ON CONFLICT update
    set_parts = ", ".join(f"{k} = excluded.{k}" for k in filtered)
    columns = ", ".join(["provider_id", "model_id", "updated_at"] + list(filtered.keys()))
    placeholders = ", ".join(["?", "?", "CURRENT_TIMESTAMP"] + ["?"] * len(filtered))

    sql = f"""
        INSERT INTO provider_scores ({columns})
        VALUES ({placeholders})
        ON CONFLICT(provider_id, model_id) DO UPDATE SET
            updated_at = CURRENT_TIMESTAMP,
            {set_parts}
    """
    await db.execute(sql, (provider_id, model_id, *filtered.values()))
    await db.commit()


async def get_provider_scores(db: aiosqlite.Connection) -> list[dict[str, Any]]:
    """Return all provider score rows."""
    cursor = await db.execute("SELECT * FROM provider_scores ORDER BY composite_score DESC")
    rows = await cursor.fetchall()
    return [_row_to_dict(r) for r in rows]


async def get_provider_score(
    db: aiosqlite.Connection,
    provider_id: str,
    model_id: str,
) -> dict[str, Any] | None:
    """Return a single provider score or None."""
    cursor = await db.execute(
        "SELECT * FROM provider_scores WHERE provider_id = ? AND model_id = ?",
        (provider_id, model_id),
    )
    row = await cursor.fetchone()
    return _row_to_dict(row) if row else None


# ---------------------------------------------------------------------------
# Discovered APIs
# ---------------------------------------------------------------------------

async def record_discovery(
    db: aiosqlite.Connection,
    source_name: str,
    provider_name: str | None,
    base_url: str | None,
    raw_data: str | None,
    parsed_data: str | None,
    status: str = "pending",
) -> int:
    """Insert a discovered-API record. Returns the new row id."""
    cursor = await db.execute(
        """
        INSERT INTO discovered_apis
            (source_name, provider_name, base_url, raw_data, parsed_data, status)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (source_name, provider_name, base_url, raw_data, parsed_data, status),
    )
    await db.commit()
    return cursor.lastrowid  # type: ignore[return-value]


async def get_pending_discoveries(db: aiosqlite.Connection) -> list[dict[str, Any]]:
    """Return all discoveries with status='pending'."""
    cursor = await db.execute(
        "SELECT * FROM discovered_apis WHERE status = 'pending' ORDER BY discovered_at DESC"
    )
    rows = await cursor.fetchall()
    return [_row_to_dict(r) for r in rows]


async def update_discovery_status(
    db: aiosqlite.Connection,
    discovery_id: int,
    status: str,
    rejection_reason: str | None = None,
) -> None:
    """Update the status (and optional rejection reason) of a discovery."""
    if status == "validated":
        await db.execute(
            """
            UPDATE discovered_apis
            SET status = ?, validated_at = CURRENT_TIMESTAMP, rejection_reason = ?
            WHERE id = ?
            """,
            (status, rejection_reason, discovery_id),
        )
    else:
        await db.execute(
            "UPDATE discovered_apis SET status = ?, rejection_reason = ? WHERE id = ?",
            (status, rejection_reason, discovery_id),
        )
    await db.commit()


# ---------------------------------------------------------------------------
# Proxy API Keys
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Event Log
# ---------------------------------------------------------------------------


async def log_event(
    db: aiosqlite.Connection,
    event_type: str,
    message: str,
    provider: str | None = None,
    model: str | None = None,
    latency_ms: float | None = None,
    tokens: int | None = None,
    api_key_id: str | None = None,
    error: str | None = None,
) -> None:
    """Write an event to the log."""
    await db.execute(
        """INSERT INTO event_log (type, message, provider, model, latency_ms, tokens, api_key_id, error)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (event_type, message, provider, model, latency_ms, tokens, api_key_id, error),
    )
    await db.commit()


async def get_recent_logs(
    db: aiosqlite.Connection,
    limit: int = 50,
    event_type: str | None = None,
) -> list[dict[str, Any]]:
    """Return recent log entries."""
    if event_type:
        cursor = await db.execute(
            "SELECT * FROM event_log WHERE type = ? ORDER BY timestamp DESC LIMIT ?",
            (event_type, limit),
        )
    else:
        cursor = await db.execute(
            "SELECT * FROM event_log ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        )
    rows = await cursor.fetchall()
    return [_row_to_dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Proxy API Keys
# ---------------------------------------------------------------------------


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


async def create_api_key(
    db: aiosqlite.Connection,
    name: str,
    rate_limit_rpm: int = 30,
) -> dict[str, Any]:
    """Create a new proxy API key. Returns the full key (only shown once)."""
    raw_key = "sk-" + secrets.token_hex(24)
    key_id = raw_key[:12] + "..."
    key_hash = _hash_key(raw_key)

    await db.execute(
        """
        INSERT INTO api_keys (key_id, key_hash, key_raw, name, rate_limit_rpm)
        VALUES (?, ?, ?, ?, ?)
        """,
        (key_id, key_hash, raw_key, name, rate_limit_rpm),
    )
    await db.commit()
    return {
        "key_id": key_id,
        "raw_key": raw_key,
        "name": name,
        "rate_limit_rpm": rate_limit_rpm,
    }


async def validate_api_key(
    db: aiosqlite.Connection,
    raw_key: str,
) -> dict[str, Any] | None:
    """Validate a raw API key. Returns key record if valid, None otherwise."""
    key_hash = _hash_key(raw_key)
    cursor = await db.execute(
        "SELECT * FROM api_keys WHERE key_hash = ? AND enabled = 1",
        (key_hash,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    await db.execute(
        "UPDATE api_keys SET last_used_at = CURRENT_TIMESTAMP WHERE key_hash = ?",
        (key_hash,),
    )
    await db.commit()
    return _row_to_dict(row)


async def list_proxy_api_keys(
    db: aiosqlite.Connection,
) -> list[dict[str, Any]]:
    """List all proxy API keys (without hashes)."""
    cursor = await db.execute(
        """
        SELECT key_id, key_raw, name, created_at, last_used_at, enabled,
               rate_limit_rpm, total_requests, total_tokens
        FROM api_keys WHERE enabled = 1 ORDER BY created_at DESC
        """
    )
    rows = await cursor.fetchall()
    return [_row_to_dict(r) for r in rows]


async def revoke_api_key(
    db: aiosqlite.Connection,
    key_id: str,
) -> bool:
    """Delete an API key. Returns True if found."""
    cursor = await db.execute(
        "DELETE FROM api_keys WHERE key_id = ?",
        (key_id,),
    )
    await db.commit()
    return cursor.rowcount > 0


async def record_api_key_usage(
    db: aiosqlite.Connection,
    key_id: str,
    tokens: int = 0,
) -> None:
    """Increment usage counters for an API key."""
    await db.execute(
        """
        UPDATE api_keys
        SET total_requests = total_requests + 1,
            total_tokens = total_tokens + ?
        WHERE key_id = ?
        """,
        (tokens, key_id),
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Admin Users & Sessions
# ---------------------------------------------------------------------------

def _hash_password(password: str, salt: str) -> str:
    """Hash a password with salt using SHA-256."""
    return hashlib.sha256((salt + password).encode()).hexdigest()


async def create_admin_user(
    db: aiosqlite.Connection,
    username: str,
    password: str,
    display_name: str | None = None,
) -> dict[str, Any]:
    """Create an admin user. Returns user record (without password_hash)."""
    salt = secrets.token_hex(16)
    pw_hash = _hash_password(password, salt)
    cursor = await db.execute(
        """
        INSERT INTO admin_users (username, password_hash, salt, display_name)
        VALUES (?, ?, ?, ?)
        """,
        (username, pw_hash, salt, display_name or username),
    )
    await db.commit()
    return {
        "id": cursor.lastrowid,
        "username": username,
        "display_name": display_name or username,
    }


async def validate_admin_login(
    db: aiosqlite.Connection,
    username: str,
    password: str,
) -> dict[str, Any] | None:
    """Validate admin credentials. Returns user record or None."""
    cursor = await db.execute(
        "SELECT * FROM admin_users WHERE username = ? AND enabled = 1",
        (username,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    user = _row_to_dict(row)
    if _hash_password(password, user["salt"]) != user["password_hash"]:
        return None
    return {"id": user["id"], "username": user["username"], "display_name": user["display_name"]}


async def change_admin_password(
    db: aiosqlite.Connection,
    user_id: int,
    new_password: str,
) -> None:
    """Change an admin user's password."""
    salt = secrets.token_hex(16)
    pw_hash = _hash_password(new_password, salt)
    await db.execute(
        "UPDATE admin_users SET password_hash = ?, salt = ? WHERE id = ?",
        (pw_hash, salt, user_id),
    )
    await db.commit()


async def admin_user_count(db: aiosqlite.Connection) -> int:
    """Return count of admin users."""
    cursor = await db.execute("SELECT COUNT(*) FROM admin_users")
    row = await cursor.fetchone()
    return row[0]


async def create_admin_session(
    db: aiosqlite.Connection,
    user_id: int,
    ttl_hours: int = 168,
) -> str:
    """Create a session token. Returns the token string."""
    token = secrets.token_hex(32)
    await db.execute(
        """
        INSERT INTO admin_sessions (token, user_id, expires_at)
        VALUES (?, ?, datetime('now', '+' || ? || ' hours'))
        """,
        (token, user_id, ttl_hours),
    )
    await db.commit()
    return token


async def validate_admin_session(
    db: aiosqlite.Connection,
    token: str,
) -> dict[str, Any] | None:
    """Validate a session token. Returns user record or None if expired/invalid."""
    cursor = await db.execute(
        """
        SELECT u.id, u.username, u.display_name
        FROM admin_sessions s
        JOIN admin_users u ON s.user_id = u.id
        WHERE s.token = ? AND s.expires_at > datetime('now') AND u.enabled = 1
        """,
        (token,),
    )
    row = await cursor.fetchone()
    return _row_to_dict(row) if row else None


async def delete_admin_session(db: aiosqlite.Connection, token: str) -> None:
    """Delete a session (logout)."""
    await db.execute("DELETE FROM admin_sessions WHERE token = ?", (token,))
    await db.commit()


async def list_admin_users(db: aiosqlite.Connection) -> list[dict[str, Any]]:
    """List all admin users."""
    cursor = await db.execute(
        "SELECT id, username, display_name, created_at, enabled FROM admin_users ORDER BY created_at"
    )
    rows = await cursor.fetchall()
    return [_row_to_dict(r) for r in rows]


async def delete_admin_user(db: aiosqlite.Connection, user_id: int) -> bool:
    """Delete an admin user and their sessions. Returns True if found."""
    await db.execute("DELETE FROM admin_sessions WHERE user_id = ?", (user_id,))
    cursor = await db.execute("DELETE FROM admin_users WHERE id = ?", (user_id,))
    await db.commit()
    return cursor.rowcount > 0


# ============================================================================
# Provider API Keys (persistent storage)
# ============================================================================


async def save_provider_api_key(
    db: aiosqlite.Connection, env_name: str, key_value: str, provider_id: str | None = None
) -> None:
    """Save or update a provider API key in the database."""
    await db.execute(
        """
        INSERT OR REPLACE INTO provider_api_keys (env_name, key_value, provider_id, updated_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        """,
        (env_name, key_value, provider_id),
    )
    await db.commit()


async def get_all_provider_api_keys(db: aiosqlite.Connection) -> dict[str, str]:
    """Get all provider API keys from database as env_name -> key_value."""
    cursor = await db.execute(
        "SELECT env_name, key_value FROM provider_api_keys WHERE enabled = 1"
    )
    rows = await cursor.fetchall()
    return {row[0]: row[1] for row in rows}


async def get_provider_api_key(db: aiosqlite.Connection, env_name: str) -> str | None:
    """Get a specific provider API key by env_name."""
    cursor = await db.execute(
        "SELECT key_value FROM provider_api_keys WHERE env_name = ? AND enabled = 1",
        (env_name,),
    )
    row = await cursor.fetchone()
    return row[0] if row else None


async def delete_provider_api_key(db: aiosqlite.Connection, env_name: str) -> bool:
    """Delete a provider API key. Returns True if found."""
    cursor = await db.execute(
        "DELETE FROM provider_api_keys WHERE env_name = ?", (env_name,)
    )
    await db.commit()
    return cursor.rowcount > 0
