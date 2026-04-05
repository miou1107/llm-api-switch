"""Database migrations — creates all required tables and indexes."""

from __future__ import annotations

import aiosqlite

_TABLES = [
    """
    CREATE TABLE IF NOT EXISTS health_checks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider_id TEXT NOT NULL,
        model_id TEXT NOT NULL,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        latency_ms REAL,
        success BOOLEAN,
        error_type TEXT,
        output_quality_score REAL,
        tokens_used INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quota_usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider_id TEXT NOT NULL,
        model_id TEXT NOT NULL,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        requests_count INTEGER DEFAULT 1,
        tokens_consumed INTEGER DEFAULT 0,
        window_type TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS discovered_apis (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_name TEXT NOT NULL,
        provider_name TEXT,
        base_url TEXT,
        raw_data TEXT,
        parsed_data TEXT,
        status TEXT DEFAULT 'pending',
        discovered_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        validated_at DATETIME,
        rejection_reason TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS provider_scores (
        provider_id TEXT NOT NULL,
        model_id TEXT NOT NULL,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        latency_p50_ms REAL DEFAULT 0,
        success_rate REAL DEFAULT 1.0,
        quality_score REAL DEFAULT 0.5,
        quota_remaining_pct REAL DEFAULT 1.0,
        composite_score REAL DEFAULT 0.5,
        PRIMARY KEY (provider_id, model_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS api_keys (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        key_id TEXT UNIQUE NOT NULL,
        key_hash TEXT NOT NULL,
        key_raw TEXT,
        name TEXT NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        last_used_at DATETIME,
        enabled BOOLEAN DEFAULT 1,
        rate_limit_rpm INTEGER DEFAULT 30,
        total_requests INTEGER DEFAULT 0,
        total_tokens INTEGER DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS admin_users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        salt TEXT NOT NULL,
        display_name TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        enabled BOOLEAN DEFAULT 1
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS admin_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        token TEXT UNIQUE NOT NULL,
        user_id INTEGER NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        expires_at DATETIME NOT NULL,
        FOREIGN KEY (user_id) REFERENCES admin_users(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS provider_api_keys (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        env_name TEXT UNIQUE NOT NULL,
        key_value TEXT NOT NULL,
        provider_id TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        enabled BOOLEAN DEFAULT 1
    )
    """,
]

_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_hc_provider ON health_checks (provider_id)",
    "CREATE INDEX IF NOT EXISTS idx_hc_model ON health_checks (model_id)",
    "CREATE INDEX IF NOT EXISTS idx_hc_timestamp ON health_checks (timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_hc_provider_model ON health_checks (provider_id, model_id)",
    "CREATE INDEX IF NOT EXISTS idx_qu_provider ON quota_usage (provider_id)",
    "CREATE INDEX IF NOT EXISTS idx_qu_model ON quota_usage (model_id)",
    "CREATE INDEX IF NOT EXISTS idx_qu_timestamp ON quota_usage (timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_qu_provider_model ON quota_usage (provider_id, model_id)",
    "CREATE INDEX IF NOT EXISTS idx_as_token ON admin_sessions (token)",
    "CREATE INDEX IF NOT EXISTS idx_pak_env ON provider_api_keys (env_name)",
]


async def run_migrations(db: aiosqlite.Connection) -> None:
    """Create all tables and indexes if they don't exist."""
    for ddl in _TABLES:
        await db.execute(ddl)
    for idx in _INDEXES:
        await db.execute(idx)
    # Event log table
    await db.execute("""
        CREATE TABLE IF NOT EXISTS event_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            type TEXT NOT NULL,
            message TEXT NOT NULL,
            provider TEXT,
            model TEXT,
            latency_ms REAL,
            tokens INTEGER,
            api_key_id TEXT,
            error TEXT
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_el_timestamp ON event_log (timestamp)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_el_type ON event_log (type)")
    # Add key_raw column if missing (migration for existing DBs)
    try:
        await db.execute("ALTER TABLE api_keys ADD COLUMN key_raw TEXT")
    except Exception:
        pass  # column already exists
    await db.commit()
