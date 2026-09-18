from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ACTIVE_STATUSES = ("launching", "running", "stopping")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat(timespec="seconds")


def month_key(moment: datetime | None = None) -> str:
    return (moment or utc_now()).strftime("%Y-%m")


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def initialize(self) -> None:
        with closing(self.connect()) as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    owner_username TEXT NOT NULL DEFAULT 'local',
                    status TEXT NOT NULL,
                    sandbox_id TEXT,
                    notebook_url TEXT,
                    notebook_token TEXT,
                    gpu_key TEXT NOT NULL,
                    cpus INTEGER NOT NULL,
                    memory_gib INTEGER NOT NULL,
                    hourly_rate REAL NOT NULL,
                    max_runtime_seconds INTEGER NOT NULL,
                    idle_timeout_minutes INTEGER NOT NULL,
                    started_at TEXT,
                    ended_at TEXT,
                    month_key TEXT NOT NULL,
                    reserved_usd REAL NOT NULL DEFAULT 0,
                    billed_usd REAL NOT NULL DEFAULT 0,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS sessions_month_idx ON sessions(month_key);
                CREATE TABLE IF NOT EXISTS datasets (
                    id TEXT PRIMARY KEY,
                    owner_username TEXT NOT NULL DEFAULT 'local',
                    name TEXT NOT NULL,
                    path TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    uploaded_at TEXT NOT NULL,
                    UNIQUE(owner_username, path)
                );
                CREATE TABLE IF NOT EXISTS users (
                    username TEXT PRIMARY KEY,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS modal_credentials (
                    username TEXT PRIMARY KEY,
                    token_id_ciphertext TEXT NOT NULL,
                    token_secret_ciphertext TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS kernels (
                    id TEXT PRIMARY KEY,
                    owner_username TEXT NOT NULL,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(owner_username, name)
                );
                CREATE TABLE IF NOT EXISTS kernel_versions (
                    id TEXT PRIMARY KEY,
                    owner_username TEXT NOT NULL,
                    kernel_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'uploading',
                    notebook_path TEXT NOT NULL,
                    attachments_json TEXT NOT NULL DEFAULT '[]',
                    volume_path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    file_count INTEGER NOT NULL,
                    cell_count INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(owner_username, kernel_id, version),
                    FOREIGN KEY(kernel_id) REFERENCES kernels(id)
                );
                CREATE TABLE IF NOT EXISTS kernel_runs (
                    id TEXT PRIMARY KEY,
                    owner_username TEXT NOT NULL,
                    kernel_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'benchmark',
                    status TEXT NOT NULL,
                    timeout_seconds INTEGER NOT NULL DEFAULT 3600,
                    queue_position INTEGER,
                    current_cell INTEGER,
                    total_cells INTEGER NOT NULL DEFAULT 0,
                    sandbox_id TEXT,
                    started_at TEXT,
                    ended_at TEXT,
                    exit_code INTEGER,
                    failure_reason TEXT,
                    output_path TEXT NOT NULL,
                    logs TEXT NOT NULL DEFAULT '',
                    events TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(kernel_id) REFERENCES kernels(id)
                );
                CREATE INDEX IF NOT EXISTS kernel_runs_owner_kernel_idx
                    ON kernel_runs(owner_username, kernel_id, created_at);
                """
            )
            # Upgrade databases created by the original single-user prototype.
            for table in ("sessions", "datasets"):
                columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                if "owner_username" not in columns:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN owner_username TEXT NOT NULL DEFAULT 'local'")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS sessions_owner_month_idx ON sessions(owner_username, month_key)"
            )

    def create_reservation(self, *, session: dict[str, Any], monthly_limit_usd: float) -> tuple[bool, str | None]:
        """Atomically enforce one active session and a per-user monthly estimate cap."""
        owner = session.get("owner_username", "local")
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            active = conn.execute(
                f"SELECT id FROM sessions WHERE owner_username = ? AND status IN ({','.join('?' for _ in ACTIVE_STATUSES)})",
                (owner, *ACTIVE_STATUSES),
            ).fetchone()
            if active:
                conn.rollback()
                return False, "A session is already launching or running."
            used = conn.execute(
                "SELECT COALESCE(SUM(billed_usd), 0) AS total FROM sessions WHERE owner_username = ? AND month_key = ?",
                (owner, session["month_key"]),
            ).fetchone()["total"]
            reserved = conn.execute(
                "SELECT COALESCE(SUM(reserved_usd), 0) AS total FROM sessions "
                "WHERE owner_username = ? AND month_key = ? AND status IN (?, ?, ?)",
                (owner, session["month_key"], *ACTIVE_STATUSES),
            ).fetchone()["total"]
            if used + reserved + session["reserved_usd"] > monthly_limit_usd + 1e-9:
                available = max(0.0, monthly_limit_usd - used - reserved)
                conn.rollback()
                return False, (
                    f"Budget guard blocked this launch. App-estimated headroom is USD {available:.2f}; "
                    f"this session reserves USD {session['reserved_usd']:.2f}."
                )
            columns = ", ".join(session.keys())
            placeholders = ", ".join("?" for _ in session)
            conn.execute(
                f"INSERT INTO sessions ({columns}) VALUES ({placeholders})",
                tuple(session.values()),
            )
            conn.commit()
            return True, None
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def update_session(self, session_id: str, **fields: Any) -> None:
        if not fields:
            return
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with closing(self.connect()) as conn:
            conn.execute(f"UPDATE sessions SET {assignments} WHERE id = ?", (*fields.values(), session_id))

    def get_session(self, session_id: str, owner_username: str = "local") -> dict[str, Any] | None:
        with closing(self.connect()) as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id = ? AND owner_username = ?",
                (session_id, owner_username),
            ).fetchone()
        return dict(row) if row else None

    def get_session_by_sandbox(self, sandbox_id: str, owner_username: str = "local") -> dict[str, Any] | None:
        if not sandbox_id:
            return None
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT * FROM sessions WHERE sandbox_id=? AND owner_username=?",
                               (sandbox_id, owner_username)).fetchone()
        return dict(row) if row else None

    def list_sessions(self, limit: int = 30, owner_username: str = "local") -> list[dict[str, Any]]:
        with closing(self.connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM sessions WHERE owner_username = ? ORDER BY COALESCE(started_at, '') DESC LIMIT ?",
                (owner_username, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def monthly_usage(self, current_month: str, owner_username: str = "local") -> dict[str, float]:
        with closing(self.connect()) as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(billed_usd), 0) AS spent, "
                "COALESCE(SUM(CASE WHEN status IN (?, ?, ?) THEN reserved_usd ELSE 0 END), 0) AS reserved "
                "FROM sessions WHERE owner_username = ? AND month_key = ?",
                (*ACTIVE_STATUSES, owner_username, current_month),
            ).fetchone()
        return {"spent_usd": float(row["spent"]), "reserved_usd": float(row["reserved"])}

    def add_dataset(self, dataset: dict[str, Any], owner_username: str = "local") -> None:
        with closing(self.connect()) as conn:
            conn.execute(
                "INSERT INTO datasets (id, owner_username, name, path, size_bytes, uploaded_at) VALUES (?, ?, ?, ?, ?, ?)",
                (dataset["id"], owner_username, dataset["name"], dataset["path"], dataset["size_bytes"], dataset["uploaded_at"]),
            )

    def list_datasets(self, owner_username: str = "local") -> list[dict[str, Any]]:
        with closing(self.connect()) as conn:
            rows = conn.execute(
                "SELECT id, name, path, size_bytes, uploaded_at FROM datasets WHERE owner_username = ? ORDER BY uploaded_at DESC",
                (owner_username,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_dataset(self, dataset_id: str, owner_username: str = "local") -> dict[str, Any] | None:
        with closing(self.connect()) as conn:
            row = conn.execute(
                "SELECT id, name, path, size_bytes, uploaded_at FROM datasets WHERE id = ? AND owner_username = ?",
                (dataset_id, owner_username),
            ).fetchone()
        return dict(row) if row else None

    def remove_dataset(self, dataset_id: str, owner_username: str = "local") -> None:
        with closing(self.connect()) as conn:
            conn.execute("DELETE FROM datasets WHERE id = ? AND owner_username = ?", (dataset_id, owner_username))

    def create_user(self, username: str, password_hash: str) -> None:
        with closing(self.connect()) as conn:
            conn.execute(
                "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
                (username, password_hash, iso_now()),
            )

    def get_user(self, username: str) -> dict[str, Any] | None:
        with closing(self.connect()) as conn:
            row = conn.execute(
                "SELECT username, password_hash, created_at FROM users WHERE username = ?", (username,)
            ).fetchone()
        return dict(row) if row else None

    def set_modal_credentials(self, username: str, token_id_ciphertext: str, token_secret_ciphertext: str) -> None:
        with closing(self.connect()) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO users (username, password_hash, created_at) VALUES (?, '', ?)",
                (username, iso_now()),
            )
            conn.execute(
                "INSERT INTO modal_credentials (username, token_id_ciphertext, token_secret_ciphertext, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(username) DO UPDATE SET "
                "token_id_ciphertext=excluded.token_id_ciphertext, "
                "token_secret_ciphertext=excluded.token_secret_ciphertext, updated_at=excluded.updated_at",
                (username, token_id_ciphertext, token_secret_ciphertext, iso_now()),
            )

    def get_modal_credentials(self, username: str) -> dict[str, str] | None:
        with closing(self.connect()) as conn:
            row = conn.execute(
                "SELECT token_id_ciphertext, token_secret_ciphertext, updated_at "
                "FROM modal_credentials WHERE username = ?", (username,)
            ).fetchone()
        return dict(row) if row else None

    def delete_modal_credentials(self, username: str) -> None:
        with closing(self.connect()) as conn:
            conn.execute("DELETE FROM modal_credentials WHERE username = ?", (username,))

    def create_kernel(self, kernel_id: str, name: str, owner_username: str = "local") -> dict[str, Any]:
        created_at = iso_now()
        with closing(self.connect()) as conn:
            conn.execute("INSERT INTO kernels (id, owner_username, name, created_at) VALUES (?, ?, ?, ?)",
                         (kernel_id, owner_username, name, created_at))
        return {"id": kernel_id, "owner_username": owner_username, "name": name, "created_at": created_at}

    def list_kernels(self, owner_username: str = "local") -> list[dict[str, Any]]:
        with closing(self.connect()) as conn:
            rows = conn.execute(
                "SELECT k.id, k.name, k.created_at, COALESCE(MAX(v.version), 0) AS latest_version, "
                "(SELECT COUNT(*) FROM kernel_runs r WHERE r.kernel_id=k.id AND r.owner_username=k.owner_username) AS run_count "
                "FROM kernels k LEFT JOIN kernel_versions v ON v.kernel_id=k.id AND v.owner_username=k.owner_username "
                "WHERE k.owner_username=? GROUP BY k.id ORDER BY k.created_at DESC", (owner_username,)
            ).fetchall()
        return [dict(row) for row in rows]

    def get_kernel(self, kernel_id: str, owner_username: str = "local") -> dict[str, Any] | None:
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT id, name, created_at FROM kernels WHERE id=? AND owner_username=?",
                               (kernel_id, owner_username)).fetchone()
        return dict(row) if row else None

    def create_kernel_version(self, version: dict[str, Any], owner_username: str = "local") -> dict[str, Any]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            kernel = conn.execute("SELECT id FROM kernels WHERE id=? AND owner_username=?",
                                  (version["kernel_id"], owner_username)).fetchone()
            if not kernel:
                conn.rollback()
                raise KeyError("Kernel not found.")
            latest = conn.execute("SELECT COALESCE(MAX(version), 0) FROM kernel_versions WHERE kernel_id=? AND owner_username=?",
                                  (version["kernel_id"], owner_username)).fetchone()[0]
            record = {**version, "version": latest + 1, "owner_username": owner_username}
            columns = ", ".join(record.keys())
            placeholders = ", ".join("?" for _ in record)
            conn.execute(f"INSERT INTO kernel_versions ({columns}) VALUES ({placeholders})", tuple(record.values()))
            conn.commit()
            return record
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def list_kernel_versions(self, kernel_id: str, owner_username: str = "local") -> list[dict[str, Any]]:
        with closing(self.connect()) as conn:
            rows = conn.execute(
                "SELECT id, kernel_id, version, status, notebook_path, attachments_json, volume_path, sha256, size_bytes, file_count, cell_count, created_at "
                "FROM kernel_versions WHERE kernel_id=? AND owner_username=? ORDER BY version", (kernel_id, owner_username)
            ).fetchall()
        return [dict(row) for row in rows]

    def get_kernel_version(self, kernel_id: str, version: int, owner_username: str = "local") -> dict[str, Any] | None:
        with closing(self.connect()) as conn:
            row = conn.execute(
                "SELECT id, kernel_id, version, status, notebook_path, attachments_json, volume_path, sha256, size_bytes, file_count, cell_count, created_at "
                "FROM kernel_versions WHERE kernel_id=? AND version=? AND owner_username=?", (kernel_id, version, owner_username)
            ).fetchone()
        return dict(row) if row else None

    def update_kernel_version(self, version_id: str, **fields: Any) -> None:
        if not fields:
            return
        assignments = ", ".join(f"{key}=?" for key in fields)
        with closing(self.connect()) as conn:
            conn.execute(f"UPDATE kernel_versions SET {assignments} WHERE id=?", (*fields.values(), version_id))

    def create_kernel_run(self, run: dict[str, Any]) -> dict[str, Any]:
        record = {"logs": "", "events": "[]", "status": "queued", "queue_position": None,
                  "current_cell": None, "total_cells": 0, "started_at": None, "ended_at": None,
                  "exit_code": None, "failure_reason": None, **run}
        columns = ", ".join(record.keys())
        placeholders = ", ".join("?" for _ in record)
        with closing(self.connect()) as conn:
            conn.execute(f"INSERT INTO kernel_runs ({columns}) VALUES ({placeholders})", tuple(record.values()))
        return record

    def get_kernel_run(self, run_id: str, owner_username: str = "local") -> dict[str, Any] | None:
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT * FROM kernel_runs WHERE id=? AND owner_username=?", (run_id, owner_username)).fetchone()
        if not row:
            return None
        record = dict(row)
        record["events"] = json.loads(record["events"] or "[]")
        return record

    def list_kernel_runs(self, kernel_id: str, owner_username: str = "local") -> list[dict[str, Any]]:
        with closing(self.connect()) as conn:
            ids = conn.execute("SELECT id FROM kernel_runs WHERE kernel_id=? AND owner_username=? ORDER BY created_at DESC",
                                (kernel_id, owner_username)).fetchall()
        return [self.get_kernel_run(row["id"], owner_username) for row in ids]

    def update_kernel_run(self, run_id: str, **fields: Any) -> None:
        if not fields:
            return
        assignments = ", ".join(f"{key}=?" for key in fields)
        with closing(self.connect()) as conn:
            conn.execute(f"UPDATE kernel_runs SET {assignments} WHERE id=?", (*fields.values(), run_id))

    def append_kernel_run_log(self, run_id: str, line: str, *, event: dict[str, Any] | None = None) -> None:
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT logs, events FROM kernel_runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                return
            logs = (row["logs"] + line)[-2_000_000:]
            events = json.loads(row["events"] or "[]")
            if event:
                events.append({"at": iso_now(), **event})
                events = events[-2000:]
            conn.execute("UPDATE kernel_runs SET logs=?, events=? WHERE id=?", (logs, json.dumps(events), run_id))

    def export_debug_state(self) -> str:
        """Small support view that deliberately excludes session tokens and URLs."""
        with closing(self.connect()) as conn:
            sessions = [
                dict(row)
                for row in conn.execute(
                    "SELECT id, owner_username, status, gpu_key, cpus, memory_gib, month_key, reserved_usd, "
                    "billed_usd FROM sessions ORDER BY rowid DESC LIMIT 30"
                )
            ]
        return json.dumps(sessions)
