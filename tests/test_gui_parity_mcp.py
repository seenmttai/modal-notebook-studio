from __future__ import annotations

import importlib
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from notebook_studio import cli as cli_module


try:
    module = importlib.import_module("notebook_studio.mcp_server")
except RuntimeError:
    # Keep MCP optional while still exercising the plain Python tool bodies in the base test extra.
    mcp_package = types.ModuleType("mcp")
    mcp_server_package = types.ModuleType("mcp.server")

    class _TestMCPServer:
        def __init__(self, *args, **kwargs):
            pass

        def tool(self, *args, **kwargs):
            return lambda function: function

        def resource(self, *args, **kwargs):
            return lambda function: function

        def prompt(self, *args, **kwargs):
            return lambda function: function

        def run(self, *args, **kwargs):
            raise AssertionError("The MCP transport must not start during unit tests.")

    mcp_server_package.MCPServer = _TestMCPServer
    mcp_package.server = mcp_server_package
    sys.modules["mcp"] = mcp_package
    sys.modules["mcp.server"] = mcp_server_package
    module = importlib.import_module("notebook_studio.mcp_server")


class _FakeClient:
    def __init__(self, upload_limit=8, dashboard_data=None, session_response=None):
        self.upload_limit = upload_limit
        self.dashboard_data = dashboard_data
        self.session_response = session_response or {}
        self.requests = []
        self.uploads = []

    def json(self, method, path, *args, **kwargs):
        self.requests.append((method, path, args, kwargs))
        if path == "/api/dashboard":
            return self.dashboard_data or {
                "upload_limit_bytes": self.upload_limit,
                "username": "alice",
                "modal_credentials_connected": True,
                "modal_credentials_saved": True,
                "workspace_volume_name": "alice-volume",
            }
        if method == "POST" and path == "/api/sessions":
            return self.session_response
        raise AssertionError(f"Unexpected API call: {method} {path}")

    def upload(self, path, **kwargs):
        file_field = kwargs["files"][0]
        payload = file_field[1][1].read()
        self.uploads.append((path, kwargs.get("fields"), file_field[1][0], payload))
        return {"ok": True, "path": path}

    def collect_events(self, path, *, max_wait_seconds=20):
        self.requests.append(("GET", path, (), {"max_wait_seconds": max_wait_seconds}))
        return {"events": [{"event": "status", "data": {"status": "complete"}}],
                "complete": True, "timed_out": False, "max_wait_seconds": max_wait_seconds}


class MCPGuiParityTests(unittest.TestCase):
    def with_client(self, client, callback):
        with patch.object(module, "_with_client", side_effect=lambda operation: operation(client)):
            return callback()

    def test_gui_account_identity_and_connection_are_available_without_secrets(self):
        client = _FakeClient()
        actual = self.with_client(client, module.studio_account_status)
        self.assertEqual(actual, {
            "username": "alice",
            "modal_connected": True,
            "modal_credentials_saved": True,
            "workspace_volume_name": "alice-volume",
        })
        self.assertNotIn("token_secret", actual)
        self.assertEqual(client.requests[0][0:2], ("GET", "/api/dashboard"))

    def test_dataset_and_storage_upload_enforce_gui_file_constraints(self):
        client = _FakeClient(upload_limit=8)
        with TemporaryDirectory() as temp:
            root = Path(temp)
            valid = root / "sample.csv"
            valid.write_bytes(b"12345678")
            dataset_result = self.with_client(client, lambda: module.dataset_upload(str(valid)))
            self.assertTrue(dataset_result["ok"])
            self.assertEqual(client.uploads[-1], ("/api/datasets", None, "sample.csv", b"12345678"))

            workspace = root / "model.bin"
            workspace.write_bytes(b"abc")
            storage_result = self.with_client(
                client, lambda: module.storage_upload(str(workspace), "/models")
            )
            self.assertTrue(storage_result["ok"])
            self.assertEqual(
                client.uploads[-1], ("/api/storage/files", {"path": "/models"}, "model.bin", b"abc")
            )

            too_large = root / "large.bin"
            too_large.write_bytes(b"123456789")
            for operation in (
                lambda: module.dataset_upload(str(too_large)),
                lambda: module.storage_upload(str(too_large), "/outputs"),
            ):
                with self.assertRaisesRegex(ValueError, "upload limit"):
                    self.with_client(client, operation)
            self.assertEqual(len(client.uploads), 2)

            empty = root / "empty.bin"
            empty.touch()
            with self.assertRaisesRegex(ValueError, "Empty files"):
                self.with_client(client, lambda: module.dataset_upload(str(empty)))
            with self.assertRaisesRegex(ValueError, "regular file"):
                self.with_client(client, lambda: module.storage_upload(str(root / "missing"), "/models"))
            self.assertEqual(len(client.uploads), 2)

    def test_session_and_benchmark_event_stream_tools_are_bounded(self):
        client = _FakeClient()
        session = self.with_client(client, lambda: module.session_events("session id", 12))
        benchmark = self.with_client(client, lambda: module.kernel_run_events("run id", 40))
        self.assertTrue(session["complete"])
        self.assertTrue(benchmark["complete"])
        self.assertEqual(client.requests[-2][1], "/api/sessions/session%20id/events")
        self.assertEqual(client.requests[-1][1], "/api/kernels/runs/run%20id/events")
        self.assertEqual(client.requests[-2][3]["max_wait_seconds"], 12)
        self.assertEqual(client.requests[-1][3]["max_wait_seconds"], 40)

    def test_status_and_start_redact_notebook_tokens_but_session_open_uses_browser(self):
        url = "https://notebook.example/lab?token=NOTEBOOK-BEARER-SECRET"
        session = {"id": "session-1", "status": "running", "notebook_url": url,
                   "notebook_token": "NOTEBOOK-BEARER-SECRET"}
        client = _FakeClient(dashboard_data={
            "username": "alice", "budget": {"active_session": session}, "sessions": [session],
        }, session_response=session)
        status = self.with_client(client, module.studio_status)
        started = self.with_client(client, lambda: module.session_start("T4"))
        self.assertNotIn("NOTEBOOK-BEARER-SECRET", repr(status))
        self.assertNotIn("notebook_url", repr(status))
        self.assertNotIn("NOTEBOOK-BEARER-SECRET", repr(started))
        self.assertNotIn("notebook_url", repr(started))

        opened = []
        with patch.object(cli_module.webbrowser, "open", side_effect=lambda value, new: opened.append((value, new)) or True):
            result = self.with_client(client, lambda: module.session_open("session-1"))
        self.assertEqual(opened, [(url, 2)])
        self.assertEqual(result, {"session_id": "session-1", "status": "running", "opened": True})
        self.assertNotIn("NOTEBOOK-BEARER-SECRET", repr(result))

    def test_visible_dataset_storage_and_account_actions_have_ai_safe_guidance(self):
        expected = (
            "dataset_list", "dataset_upload", "dataset_delete", "dataset_download",
            "storage_list", "storage_upload", "storage_download", "storage_delete",
            "modal_account_status", "modal_account_connect", "modal_account_forget",
            "studio_account_status", "session_open", "session_events", "kernel_run_events",
        )
        for name in expected:
            with self.subTest(tool=name):
                self.assertTrue(callable(getattr(module, name, None)))

        self.assertIn("machine running the MCP server", module.dataset_upload.__doc__)
        self.assertIn("upload limit", module.storage_upload.__doc__)
        self.assertIn("obtain the user's intent", module.dataset_delete.__doc__)
        self.assertIn("does not revoke", module.modal_account_forget.__doc__)
        self.assertIn("may retain them", module.modal_account_connect.__doc__)


if __name__ == "__main__":
    unittest.main()
