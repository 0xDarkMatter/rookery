"""Schema migrations for the orchestrator's SQLite queue.

Migrations are raw ``.sql`` files under :mod:`axiom.orchestrator.migrations`,
applied in lexicographic order. An ``_applied_migrations`` bookkeeping table
records which files have run so reruns are idempotent.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
BUSY_TIMEOUT_MS = 10_000


def open_connection(db_path: Path) -> sqlite3.Connection:
    """Open a SQLite connection with WAL mode and a generous busy timeout.

    WAL is a persistent per-database pragma (set once, remembered), but we set
    it on every open anyway because the first connection to a new db file needs
    it to be enabled before readers join. ``busy_timeout`` is a per-connection
    setting; we pick 10s to survive cross-process claim contention on Windows
    where SQLite locking is noisier than POSIX.
    """

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(db_path),
        timeout=BUSY_TIMEOUT_MS / 1000,
        isolation_level=None,  # manual transactions via BEGIN / COMMIT
        check_same_thread=False,
    )
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS};")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_bookkeeping(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS _applied_migrations (
            name        TEXT PRIMARY KEY,
            applied_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )


def _already_applied(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM _applied_migrations WHERE name = ?",
        (name,),
    ).fetchone()
    return row is not None


def apply_migrations(db_path: Path, migrations_dir: Path | None = None) -> list[str]:
    """Apply any pending ``.sql`` files under *migrations_dir* in sorted order.

    Args:
        db_path: SQLite file path. Created if absent.
        migrations_dir: Directory of ``.sql`` files. Defaults to the package's
            own ``migrations/`` directory.

    Returns:
        List of migration filenames applied in this invocation (empty if the
        database was already up to date).
    """

    source_dir = migrations_dir or MIGRATIONS_DIR
    if not source_dir.is_dir():
        raise FileNotFoundError(f"migrations dir not found: {source_dir}")

    conn = open_connection(db_path)
    try:
        _ensure_bookkeeping(conn)
        applied: list[str] = []
        for sql_path in sorted(source_dir.glob("*.sql")):
            name = sql_path.name
            if _already_applied(conn, name):
                continue
            script = sql_path.read_text(encoding="utf-8")
            # executescript manages its own transaction semantics; our schema
            # uses CREATE ... IF NOT EXISTS so a partial failure is safe to
            # retry on next startup.
            conn.executescript(script)
            conn.execute(
                "INSERT INTO _applied_migrations (name) VALUES (?)",
                (name,),
            )
            applied.append(name)
        return applied
    finally:
        conn.close()


__all__ = [
    "BUSY_TIMEOUT_MS",
    "MIGRATIONS_DIR",
    "apply_migrations",
    "open_connection",
]
