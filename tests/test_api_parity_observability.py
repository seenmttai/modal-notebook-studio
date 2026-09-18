from __future__ import annotations

import base64
import json
from pathlib import Path

from fastapi.testclient import TestClient

from notebook_studio.config import Settings
from notebook_studio.database import Database, iso_now, month_key, utc_now
from notebook_studio.main import create_app
from notebook_studio.pricing import hourly_rate


def make_client(tmp_path, monkeypatch):
    from notebook_studio.modal_provider import LaunchedSandbox, ModalProvider

    monkeypatch.setattr(ModalProvider, "__init__", lambda self, *args, **kwargs: None)
    monkeypatch.setattr(ModalProvider, "close", lambda self: None)
    monkeypatch.setattr(ModalProvider, "launch", lambda self, **kwargs: LaunchedSandbox(
        "sb-observe", "https://example.invalid/?token=secret", "secret"))
    monkeypatch.setattr(ModalProvider, "is_running", lambda self, sandbox_id: True)
    monkeypatch.setattr(ModalProvider, "sandbox_status", lambda self, sandbox_id: {"running": True, "exit_code": None})
    monkeypatch.setattr(ModalProvider, "last_sandbox_status", lambda self, sandbox_id: {"running": True, "exit_code": None})
    monkeypatch.setattr(ModalProvider, "resource_usage", lambda self, sandbox_id: {
        "cpu_load_1m": 0.5, "memory_total_bytes": 4096, "memory_available_bytes": 2048,
        "gpu": [{"name": "Mock GPU", "utilization_percent": 25, "memory_used_mib": 10, "memory_total_mib": 100}],
    })
    monkeypatch.setattr(ModalProvider, "tail_logs", lambda self, sandbox_id, entries=100: [
        {"timestamp": "now", "source": "stdout", "message": "Jupyter ready"},
    ])
    monkeypatch.setattr(ModalProvider, "upload", lambda self, src, dst, sandbox_id=None: None)

    config = Settings(
        password="test-password", modal_enabled=True, modal_app_name="test-app",
        modal_volume_name="test-volume", monthly_budget_usd=30.0, safety_buffer_usd=1.0,
        max_session_hours=8.0, default_idle_timeout_minutes=15,
        upload_limit_bytes=1024 * 1024, database_path=tmp_path / "api-observability.sqlite3",
        app_public=False,
    )
    db = Database(config.database_path)
    app = create_app(config, db)
    client = TestClient(app)
    client.headers["Authorization"] = "Basic " + base64.b64encode(b"me:test-password").decode()
    return client, db, ModalProvider


def test_sandbox_failure_reason_and_exit_code_survive_budget_reconciliation(tmp_path, monkeypatch):
    client, db, ModalProvider = make_client(tmp_path, monkeypatch)
    now = utc_now()
    session = {
        "id": "sb-failed", "owner_username": "local", "status": "running",
        "sandbox_id": "sb-failed", "notebook_url": None, "notebook_token": None,
        "gpu_key": "T4", "cpus": 4, "memory_gib": 32,
        "hourly_rate": hourly_rate("T4", 4, 32), "max_runtime_seconds": 3600,
        "idle_timeout_minutes": 15, "started_at": now.isoformat(), "ended_at": None,
        "month_key": month_key(now), "reserved_usd": 1.0, "billed_usd": 0.0, "error": None,
    }
    assert db.create_reservation(session=session, monthly_limit_usd=30.0)[0]
    monkeypatch.setattr(ModalProvider, "is_running", lambda self, sandbox_id: False)
    monkeypatch.setattr(ModalProvider, "last_sandbox_status", lambda self, sandbox_id: {
        "running": False, "exit_code": 17,
    })

    first = client.get("/api/sessions/sb-failed/status")
    assert first.status_code == 200
    assert first.json()["status"] == "failed"
    assert first.json()["exit_code"] == 17
    assert first.json()["failure_reason"] == "Modal Sandbox exited with code 17."
    assert first.json()["phase"] == "terminal"
    # Reconciliation is persisted, so later requests still expose the same reason/code.
    later = client.get("/api/sessions/sb-failed/status").json()
    assert later["status"] == "failed" and later["exit_code"] == 17
    assert later["failure_reason"] == first.json()["failure_reason"]
    assert client.get("/api/usage").json()["reserved_usd"] == 0


def test_nbclient_progress_markers_drive_structured_history_and_logs(tmp_path, monkeypatch):
    client, _, ModalProvider = make_client(tmp_path, monkeypatch)
    def execute(self, sandbox_id, archive_path, notebook_path, output_path, timeout_seconds, on_output):
        # Include multiple markers in one PTY chunk to cover buffering behavior.
        on_output("STUDIO_CELL_START:1/2\nSTUDIO_CELL_COMPLETE:1/2\n")
        on_output("cell output: 42\nSTUDIO_CELL_START:2/2\nSTUDIO_CELL_ERROR:2/2\nTraceback: boom\n")
        return 1
    monkeypatch.setattr(ModalProvider, "execute_notebook", execute)

    kernel = client.post("/api/kernels", json={"name": "observable"}).json()["kernel"]
    notebook = {
        "nbformat": 4, "nbformat_minor": 5, "metadata": {},
        "cells": [
            {"cell_type": "code", "metadata": {}, "execution_count": None, "outputs": [], "source": ["print(42)"]},
            {"cell_type": "code", "metadata": {}, "execution_count": None, "outputs": [], "source": ["raise RuntimeError()"]},
        ],
    }
    version = client.post(f"/api/kernels/{kernel['id']}/versions", files={
        "notebook": ("bench.ipynb", json.dumps(notebook).encode(), "application/json"),
    }).json()["version"]
    session = client.post("/api/sessions", json={
        "gpu": "T4", "cpus": 4, "memory_gib": 32, "max_hours": 0.1,
    }).json()["session"]
    assert session["status"] == "running"
    run = client.post(f"/api/kernels/{kernel['id']}/runs", json={
        "version": version["version"], "timeout_seconds": 30,
    }).json()["run"]

    status = client.get(f"/api/kernels/runs/{run['id']}").json()
    assert status["status"] == "failed" and status["exit_code"] == 1
    assert status["cell_progress"] == {"current": 2, "total": 2}
    assert "cell output: 42" in status["logs"]
    assert "STUDIO_CELL_" not in status["logs"]
    events = [event["type"] for event in status["events"]]
    assert events.count("cell_started") == 2
    assert "cell_completed" in events and "cell_error" in events


def test_executor_uses_nbclient_lifecycle_hooks_instead_of_log_text(tmp_path):
    from notebook_studio.modal_provider import notebook_execution_script

    script = notebook_execution_script()
    assert "class StreamingNotebookClient(NotebookClient)" in script
    assert "async def on_cell_start" in script
    assert "async def on_cell_complete" in script
    assert "async def on_cell_error" in script
    assert "STUDIO_CELL_START:" in script
    assert "STUDIO_CELL_COMPLETE:" in script
