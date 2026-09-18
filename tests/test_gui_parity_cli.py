"""Parity checks for GUI actions on Overview, Notebooks, Usage, and Account."""

import argparse

import httpx
import pytest

from notebook_studio import cli
from notebook_studio.cli import StudioClient, StudioError, execute, make_parser


def dashboard(*, available=12.0, sessions=None):
    return {
        "budget": {"available_usd": available},
        "gpus": [{"key": "T4", "name": "NVIDIA T4", "vram_gib": 16,
                  "gpu_usd_per_hour": 0.5904, "hourly_rate_4cpu_32gib": 1.319}],
        "cpu_choices": [2, 4, 8],
        "ram_choices_gib": [8, 16, 32],
        "cpu_usd_per_core_hour": 0.141912,
        "ram_usd_per_gib_hour": 0.024012,
        "max_session_hours": 8,
        "sessions": sessions or [],
    }


def client_for(handler):
    return StudioClient("https://studio.invalid", "user", "test-password",
                        transport=httpx.MockTransport(handler))


def test_estimate_matches_gui_gpu_resource_runtime_and_budget_controls():
    seen = []

    def handler(request):
        seen.append(request)
        assert request.url.path == "/api/dashboard"
        return httpx.Response(200, json=dashboard(available=2.0))

    client = client_for(handler)
    try:
        args = make_parser().parse_args([
            "estimate", "--gpu", "T4", "--cpus", "8", "--ram", "16", "--hours", "8",
        ])
        result = execute(args, client)
    finally:
        client.close()

    expected_hourly = 0.5904 + 8 * 0.141912 + 16 * 0.024012
    assert result["gpu"] == "T4" and result["vram_gib"] == 16
    assert result["cpus"] == 8 and result["memory_gib"] == 16
    assert result["estimated_hourly_rate_usd"] == pytest.approx(expected_hourly)
    assert result["estimated_max_hours"] == pytest.approx(2.0 / expected_hourly)
    assert result["estimated_max_cost_usd"] == pytest.approx(2.0)
    assert result["can_reserve_one_minute"] is True
    assert len(seen) == 1


def test_estimate_rejects_invalid_gpu_or_resource_choices_without_secret_output(capsys):
    client = client_for(lambda request: httpx.Response(200, json=dashboard()))
    try:
        args = make_parser().parse_args(["estimate", "--gpu", "unknown"])
        with pytest.raises(StudioError, match="Unknown GPU"):
            execute(args, client)
    finally:
        client.close()
    assert "test-password" not in capsys.readouterr().out


def test_status_list_and_start_redact_notebook_bearer_urls():
    session = {
        "id": "session-123", "status": "running",
        "notebook_url": "https://notebook.example/lab?token=NOTEBOOK-BEARER-SECRET",
        "notebook_token": "NOTEBOOK-BEARER-SECRET",
    }
    data = dashboard(sessions=[session])
    data["budget"]["active_session"] = session
    secret = "NOTEBOOK-BEARER-SECRET"

    def handler(request):
        if request.url.path == "/api/dashboard":
            return httpx.Response(200, json=data)
        if request.url.path == "/api/sessions":
            return httpx.Response(200, json=session)
        if request.url.path == "/api/usage":
            return httpx.Response(200, json={"recent": session})
        raise AssertionError(request.url)

    for argv in (
        ["status"], ["sessions", "list"],
        ["sessions", "start", "--gpu", "T4"], ["usage"],
    ):
        client = client_for(handler)
        try:
            result = execute(make_parser().parse_args(argv), client)
        finally:
            client.close()
        assert secret not in repr(result)
        assert "notebook_url" not in repr(result)
        assert "notebook_token" not in repr(result)


def test_sessions_open_uses_browser_without_printing_the_session_url(monkeypatch, capsys):
    # Deliberately token-free URL; session URLs are never returned or printed by this command.
    notebook_url = "https://notebook.example/"
    session_id = "session-123"
    captured = []
    monkeypatch.setattr(cli.webbrowser, "open", lambda url, new: captured.append((url, new)) or True)
    client = client_for(lambda request: httpx.Response(200, json=dashboard(sessions=[{
        "id": session_id, "status": "running", "notebook_url": notebook_url,
    }])))
    try:
        args = make_parser().parse_args(["sessions", "open", session_id])
        result = execute(args, client)
    finally:
        client.close()

    assert captured == [(notebook_url, 2)]
    assert result == {"session_id": session_id, "status": "running", "opened": True}
    assert notebook_url not in repr(result)
    assert notebook_url not in capsys.readouterr().out


def test_sessions_open_refuses_nonrunning_session_and_missing_browser(monkeypatch):
    client = client_for(lambda request: httpx.Response(200, json=dashboard(sessions=[{
        "id": "session-123", "status": "launching", "notebook_url": None,
    }])))
    try:
        args = make_parser().parse_args(["sessions", "open", "session-123"])
        with pytest.raises(StudioError, match="not ready"):
            execute(args, client)
    finally:
        client.close()

    monkeypatch.setattr(cli.webbrowser, "open", lambda *args, **kwargs: False)
    client = client_for(lambda request: httpx.Response(200, json=dashboard(sessions=[{
        "id": "session-123", "status": "running", "notebook_url": "https://notebook.example/",
    }])))
    try:
        args = make_parser().parse_args(["sessions", "open", "session-123"])
        with pytest.raises(StudioError, match="No browser accepted"):
            execute(args, client)
    finally:
        client.close()
