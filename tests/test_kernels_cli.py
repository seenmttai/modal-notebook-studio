from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from notebook_studio.cli import StudioClient
from notebook_studio.config import Settings
from notebook_studio.database import Database
from notebook_studio.main import create_app
from notebook_studio.pricing import estimate_cost, hourly_rate


def make_settings(tmp_path, **overrides):
    values = {
        "password": "test-password", "modal_enabled": True,
        "modal_app_name": "test-app", "modal_volume_name": "test-volume",
        "monthly_budget_usd": 30.0, "safety_buffer_usd": 1.0,
        "max_session_hours": 8.0, "default_idle_timeout_minutes": 15,
        "upload_limit_bytes": 1024 * 1024, "database_path": tmp_path / "kernels.sqlite3",
        "app_public": False,
    }
    values.update(overrides)
    return Settings(**values)


def authenticated_client(app):
    client = TestClient(app)
    auth = base64.b64encode(b"me:test-password").decode()
    client.headers["Authorization"] = f"Basic {auth}"
    return client


def patch_provider(monkeypatch, *, execute_result=0):
    from notebook_studio.modal_provider import LaunchedSandbox, ModalProvider

    monkeypatch.setattr(ModalProvider, "__init__", lambda self, *args, **kwargs: None)
    monkeypatch.setattr(ModalProvider, "close", lambda self: None)
    monkeypatch.setattr(ModalProvider, "launch", lambda self, **kwargs: LaunchedSandbox(
        "sb-kernel-test", "https://example.invalid/?token=hidden", "hidden"))
    monkeypatch.setattr(ModalProvider, "terminate", lambda self, sid: None)
    monkeypatch.setattr(ModalProvider, "is_running", lambda self, sid: True)
    monkeypatch.setattr(ModalProvider, "sandbox_status", lambda self, sid: {"running": True, "exit_code": None})
    monkeypatch.setattr(ModalProvider, "resource_usage", lambda self, sid: {
        "cpu_load_1m": 1.25, "memory_total_bytes": 1024, "memory_available_bytes": 512,
        "gpu": [{"name": "Test GPU", "utilization_percent": 42, "memory_used_mib": 100, "memory_total_mib": 1000}],
    })
    monkeypatch.setattr(ModalProvider, "tail_logs", lambda self, sid, entries=100: [
        {"timestamp": "now", "source": "stdout", "message": "ready"}])
    uploaded = []
    def upload(self, src, dst, sandbox_id=None):
        uploaded.append((Path(src).read_bytes(), dst, sandbox_id))
    monkeypatch.setattr(ModalProvider, "upload", upload)
    outcomes = [execute_result]
    def execute(self, sid, archive_path, notebook_path, output_path, timeout_seconds, on_output):
        on_output("Executing cell 0\n")
        on_output("benchmark result: 42\n")
        return outcomes.pop(0) if outcomes else 0
    monkeypatch.setattr(ModalProvider, "execute_notebook", execute)
    monkeypatch.setattr(ModalProvider, "list_volume_files", lambda self, path: [
        {"path": path + "/executed.ipynb", "size_bytes": 120, "type": "file", "modified_at": "now"},
        {"path": path + "/metrics.json", "size_bytes": 32, "type": "file", "modified_at": "now"},
    ])
    def download(self, path, destination):
        payload = uploaded[0][0] if "/kernels/" in path and uploaded else b"selected output"
        Path(destination).write_bytes(payload)
        return len(payload)
    monkeypatch.setattr(ModalProvider, "download_volume_file", download)
    return uploaded, outcomes


def test_versioned_kernel_keeps_stable_id_attachments_and_history(tmp_path, monkeypatch):
    uploaded, _ = patch_provider(monkeypatch)
    client = authenticated_client(create_app(make_settings(tmp_path), Database(tmp_path / "kernels.sqlite3")))
    kernel = client.post("/api/kernels", json={"name": "small-llm"}).json()["kernel"]
    notebook_v1 = {"nbformat": 4, "nbformat_minor": 5,
                  "metadata": {"kernelspec": {"name": "python3"}},
                  "cells": [{"cell_type": "code", "metadata": {}, "execution_count": None,
                             "outputs": [], "source": ["print('v1')"]}]}
    v1 = client.post(f"/api/kernels/{kernel['id']}/versions", files=[
        ("notebook", ("train.ipynb", json.dumps(notebook_v1).encode(), "application/json")),
        ("attachments", ("data/sample.txt", b"attachment-one", "text/plain")),
    ])
    assert v1.status_code == 200, v1.text
    first = v1.json()["version"]
    assert first["version"] == 1 and first["status"] == "ready"
    assert first["attachments"] == ["data/sample.txt"]
    assert first["file_count"] == 2 and len(first["sha256"]) == 64
    notebook_v2 = {**notebook_v1, "cells": [*notebook_v1["cells"],
                  {"cell_type": "markdown", "metadata": {}, "source": ["new version"]}]}
    v2 = client.post(f"/api/kernels/{kernel['id']}/versions", files=[
        ("notebook", ("train.ipynb", json.dumps(notebook_v2).encode(), "application/json")),
        ("attachments", ("data/sample.txt", b"attachment-two", "text/plain")),
    ]).json()["version"]
    assert v2["version"] == 2 and v2["cell_count"] == 2
    assert v2["kernel_id"] == first["kernel_id"] == kernel["id"]
    assert v2["volume_path"] != first["volume_path"]
    assert first["sha256"] != v2["sha256"]
    listing = client.get(f"/api/kernels/{kernel['id']}/versions").json()["versions"]
    assert [item["version"] for item in listing] == [1, 2]
    assert [item["attachments"] for item in listing] == [["data/sample.txt"], ["data/sample.txt"]]
    assert uploaded[0][1].endswith("/versions/1/bundle.zip")
    assert uploaded[1][1].endswith("/versions/2/bundle.zip")
    # Each version is a distinct ZIP, and its own attachment bytes survive.
    import io, zipfile
    first_zip = zipfile.ZipFile(io.BytesIO(uploaded[0][0]))
    second_zip = zipfile.ZipFile(io.BytesIO(uploaded[1][0]))
    assert first_zip.read("data/sample.txt") == b"attachment-one"
    assert second_zip.read("data/sample.txt") == b"attachment-two"
    assert first_zip.testzip() is None and second_zip.testzip() is None


def test_benchmark_status_stream_outputs_and_session_observability(tmp_path, monkeypatch):
    patch_provider(monkeypatch)
    client = authenticated_client(create_app(make_settings(tmp_path), Database(tmp_path / "run.sqlite3")))
    kernel = client.post("/api/kernels", json={"name": "bench"}).json()["kernel"]
    notebook = {"nbformat": 4, "nbformat_minor": 5, "metadata": {},
                "cells": [{"cell_type": "code", "metadata": {}, "execution_count": None,
                           "outputs": [], "source": ["print(42)"]}]}
    version = client.post(f"/api/kernels/{kernel['id']}/versions", files={
        "notebook": ("bench.ipynb", json.dumps(notebook).encode(), "application/json")}).json()["version"]
    session_response = client.post("/api/sessions", json={"gpu": "T4", "cpus": 4,
        "memory_gib": 32, "max_hours": 0.1, "idle_timeout_minutes": 15})
    assert session_response.status_code == 200, session_response.text
    session = session_response.json()["session"]
    run_response = client.post(f"/api/kernels/{kernel['id']}/runs", json={
        "version": version["version"], "timeout_seconds": 30, "kind": "benchmark"})
    assert run_response.status_code == 200, run_response.text
    run_id = run_response.json()["run"]["id"]
    status = client.get(f"/api/kernels/runs/{run_id}").json()
    assert status["status"] == "complete" and status["exit_code"] == 0
    assert status["kind"] == "benchmark" and status["version"] == 1
    assert status["cell_progress"] == {"current": 1, "total": 1}
    assert status["queue_position"] is None and status["worker_assignment"]["sandbox_id"] == "sb-kernel-test"
    assert "benchmark result: 42" in status["logs"]
    history = client.get(f"/api/kernels/{kernel['id']}/runs").json()["runs"]
    assert len(history) == 1 and history[0]["id"] == run_id
    outputs = client.get(f"/api/kernels/runs/{run_id}/outputs").json()["files"]
    assert [Path(entry["path"]).name for entry in outputs] == ["executed.ipynb", "metrics.json"]
    selected = client.get(f"/api/kernels/runs/{run_id}/outputs/download", params={"path": "metrics.json"})
    assert selected.status_code == 200 and selected.content == b"selected output"
    version_download = client.get(f"/api/kernels/{kernel['id']}/versions/1/download")
    assert version_download.status_code == 200 and zipfile_is_valid(version_download.content)
    streamed = client.get(f"/api/kernels/runs/{run_id}/events")
    assert "event: log" in streamed.text and "cell_progress" in streamed.text and "event: status" in streamed.text
    observed = client.get(f"/api/sessions/{session['id']}/status").json()
    assert observed["queue_position"] is None
    assert observed["worker_assignment"]["sandbox_id"] == "sb-kernel-test"
    assert observed["resources"]["observed"]["gpu"][0]["utilization_percent"] == 42
    assert observed["progress"]["cell_progress"] is None
    logs = client.get(f"/api/sessions/{session['id']}/logs").json()
    assert logs["logs"][0]["message"] == "ready"
    stopped = client.post(f"/api/sessions/{session['id']}/stop").json()["session"]
    assert stopped["status"] == "stopped" and stopped["reserved_usd"] == 0
    assert client.get(f"/api/kernels/runs/{run_id}/outputs/download", params={"path": "../../etc/passwd"}).status_code == 422


def test_dataset_and_generic_storage_upload_list_download_delete(tmp_path, monkeypatch):
    from notebook_studio.modal_provider import ModalProvider
    uploaded, _ = patch_provider(monkeypatch)
    deleted = []
    monkeypatch.setattr(ModalProvider, "delete_workspace_path", lambda self, path, sandbox_id=None: deleted.append(path))
    db = Database(tmp_path / "storage.sqlite3")
    client = authenticated_client(create_app(make_settings(tmp_path), db))
    dataset = client.post("/api/datasets", files={"file": ("train.csv", b"x,y\n1,2\n", "text/csv")})
    assert dataset.status_code == 200
    dataset_record = dataset.json()["dataset"]
    assert uploaded[-1][1] == "/workspace" + dataset_record["path"]
    got = client.get(f"/api/datasets/{dataset_record['id']}/download")
    assert got.status_code == 200 and got.content == b"selected output"
    listing = client.get("/api/storage", params={"path": "/models"})
    assert listing.status_code == 200 and len(listing.json()["files"]) == 2
    stored = client.post("/api/storage/files", data={"path": "/models"},
                         files={"file": ("weights.bin", b"model-bytes", "application/octet-stream")})
    assert stored.status_code == 200
    assert uploaded[-1][1] == "/workspace/models/weights.bin"
    downloaded = client.get("/api/storage/download", params={"path": "/models/weights.bin"})
    assert downloaded.status_code == 200 and downloaded.content == b"selected output"
    deleted_file = client.delete("/api/storage/files", params={"path": "/models/weights.bin"})
    assert deleted_file.status_code == 200 and deleted[-1] == "/workspace/models/weights.bin"
    assert client.post("/api/storage/files", data={"path": "/datasets"},
                       files={"file": ("untracked.txt", b"x", "text/plain")}).status_code == 422
    deleted_dataset = client.delete(f"/api/datasets/{dataset_record['id']}")
    assert deleted_dataset.status_code == 200
    assert db.list_datasets() == []


def zipfile_is_valid(data: bytes) -> bool:
    import io, zipfile
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return archive.testzip() is None and "manifest.json" in archive.namelist()


def test_cli_client_auth_error_parsing_and_streamed_download(tmp_path):
    seen = []
    def handler(request):
        seen.append(request)
        if request.url.path == "/api/usage":
            return httpx.Response(200, json={"available_usd": 7.5})
        if request.url.path == "/file":
            return httpx.Response(200, content=b"large-file-content", headers={"content-type": "application/octet-stream"})
        return httpx.Response(404, json={"detail": "missing"})
    client = StudioClient("https://studio.invalid", "alice", "secret", transport=httpx.MockTransport(handler))
    try:
        assert client.json("GET", "/api/usage")["available_usd"] == 7.5
        output = tmp_path / "artifact.bin"
        result = client.download("/file", output)
        assert result["size_bytes"] == len(b"large-file-content") and output.read_bytes() == b"large-file-content"
        with pytest.raises(Exception, match="HTTP 404: missing"):
            client.json("GET", "/missing")
    finally:
        client.close()
    assert seen[0].headers["authorization"] == "Basic " + base64.b64encode(b"alice:secret").decode()
    assert seen[1].method == "GET" and seen[1].url.path == "/file"


def test_mcp_sse_event_collector_parses_logs_and_terminal_status():
    body = (
        'event: log\ndata: {"text":"cell started"}\n\n'
        'event: progress\ndata: {"cell":2,"state":"complete"}\n\n'
        'event: status\ndata: {"status":"failed","failure_reason":"exit 1"}\n\n'
    )
    client = StudioClient(
        "https://studio.invalid", "alice", "password",
        transport=httpx.MockTransport(lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=body
        )),
    )
    try:
        result = client.collect_events("/events", max_wait_seconds=120)
    finally:
        client.close()
    assert result["complete"] is True
    assert result["timed_out"] is False
    assert result["max_wait_seconds"] == 60
    assert [item["event"] for item in result["events"]] == ["log", "progress", "status"]
    assert result["events"][-1]["data"]["failure_reason"] == "exit 1"


def test_kernel_storage_is_owner_scoped(tmp_path):
    from notebook_studio.credentials import password_hash
    db = Database(tmp_path / "owners.sqlite3")
    db.create_user("me", password_hash("test-password"))
    db.create_user("bob", password_hash("bob-password-123"))
    app = create_app(make_settings(tmp_path, multi_user_mode=True), db)
    alice = authenticated_client(app)
    bob = TestClient(app)
    bob.headers["Authorization"] = "Basic " + base64.b64encode(b"bob:bob-password-123").decode()
    kernel = alice.post("/api/kernels", json={"name": "private"}).json()["kernel"]
    assert bob.get(f"/api/kernels/{kernel['id']}/versions").status_code == 404
    assert bob.get(f"/api/kernels/{kernel['id']}/runs").status_code == 404


def test_mcp_registers_ai_first_tools_and_resources(monkeypatch):
    pytest.importorskip("mcp")
    from mcp import Client
    from notebook_studio import mcp_server
    monkeypatch.setattr(mcp_server, "_with_client", lambda operation: {
        "budget": {"available_usd": 5}, "gpus": [{"key": f"GPU-{n}"} for n in range(11)],
        "sessions": [], "datasets": []
    })
    async def exercise():
        async with Client(mcp_server.mcp) as client:
            tools = await client.list_tools()
            names = {tool.name for tool in tools.tools}
            assert {"studio_status", "session_start", "dataset_upload", "storage_list",
                    "kernel_push", "kernel_run_status", "kernel_run_outputs", "kernel_run_events",
                    "session_events", "session_open", "kernel_output_download",
                    "modal_account_connect"}.issubset(names)
            result = await client.call_tool("studio_status", {})
            assert not result.is_error
            payload = json.loads(result.content[0].text)
            assert payload["budget"]["available_usd"] == 5
            gpu_result = await client.call_tool("gpu_options", {})
            gpu_payload = [json.loads(item.text) for item in gpu_result.content]
            assert len(gpu_payload) >= 10
            resources = await client.list_resources()
            assert {item.uri for item in resources.resources} >= {"notebook-studio://dashboard", "notebook-studio://usage"}
    asyncio.run(exercise())


def test_attachment_paths_keep_notebook_relative_structure(tmp_path):
    from notebook_studio.cli import attachment_archive_name
    project = tmp_path / "project"
    (project / "data").mkdir(parents=True)
    notebook = project / "train.ipynb"
    attachment = project / "data" / "config.json"
    notebook.write_text("{}")
    attachment.write_text("{}")
    assert attachment_archive_name(notebook, attachment) == "data/config.json"
    outside = tmp_path / "elsewhere" / "config.json"
    outside.parent.mkdir()
    outside.write_text("{}")
    assert attachment_archive_name(notebook, outside) == "config.json"
