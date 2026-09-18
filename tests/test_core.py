from __future__ import annotations

import base64
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from notebook_studio.config import Settings
from notebook_studio.database import Database
from notebook_studio.main import _safe_filename, create_app
from notebook_studio.pricing import estimate_cost, hourly_rate


def settings(tmp_path, **overrides):
    values = {
        "password": "test-password",
        "modal_enabled": False,
        "modal_app_name": "test-app",
        "modal_volume_name": "test-volume",
        "monthly_budget_usd": 30.0,
        "safety_buffer_usd": 1.0,
        "max_session_hours": 8.0,
        "default_idle_timeout_minutes": 15,
        "upload_limit_bytes": 1024,
        "database_path": tmp_path / "test.sqlite3",
        "app_public": False,
    }
    values.update(overrides)
    return Settings(**values)


def client_for(tmp_path, **overrides):
    app = create_app(settings(tmp_path, **overrides), Database(tmp_path / "test.sqlite3"))
    client = TestClient(app)
    auth = base64.b64encode(b"me:test-password").decode()
    client.headers["Authorization"] = f"Basic {auth}"
    return client


def test_gpu_cost_estimate_includes_sandbox_cpu_and_ram():
    assert hourly_rate("T4", 4, 32) > 0.75
    assert estimate_cost("T4", 4, 32, 3600) == hourly_rate("T4", 4, 32)
    assert hourly_rate("H100", 4, 32) > hourly_rate("T4", 4, 32)


def test_dashboard_lists_gpu_memory_and_budget(tmp_path):
    response = client_for(tmp_path).get("/api/dashboard")
    assert response.status_code == 200
    data = response.json()
    t4 = next(item for item in data["gpus"] if item["key"] == "T4")
    assert t4["vram_gib"] == 16
    assert data["budget"]["app_limit_usd"] == 29
    assert data["budget"]["modal_enabled"] is False


def test_modal_launch_is_opt_in(tmp_path):
    response = client_for(tmp_path).post(
        "/api/sessions",
        json={"gpu": "T4", "cpus": 4, "memory_gib": 32, "max_hours": 1},
    )
    assert response.status_code == 409
    assert "token" in response.json()["detail"].lower()


def test_authentication_required(tmp_path):
    app = create_app(settings(tmp_path), Database(tmp_path / "other.sqlite3"))
    response = TestClient(app).get("/api/dashboard")
    assert response.status_code == 401



def test_worker_origin_can_preflight_local_api(tmp_path):
    worker_origin = "https://modal-notebook-studio-staging.bhansalimanan55.workers.dev"
    client = client_for(tmp_path, allowed_origins=(worker_origin,))
    response = client.options(
        "/api/sessions",
        headers={
            "Origin": worker_origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == worker_origin
    assert "authorization" in response.headers["access-control-allow-headers"].lower()


def test_local_helper_rejects_unlisted_cross_origin_writes(tmp_path):
    client = client_for(tmp_path, allowed_origins=("https://modal-notebook-studio-staging.bhansalimanan55.workers.dev",))
    response = client.post(
        "/api/sessions",
        headers={"Origin": "https://attacker.invalid"},
        json={"gpu": "T4", "cpus": 4, "memory_gib": 32, "max_hours": 1},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Cross-origin request rejected."

def test_reservations_are_atomic_and_respect_monthly_limit(tmp_path):
    db = Database(tmp_path / "budget.sqlite3")
    now = datetime(2026, 9, 17, tzinfo=timezone.utc)
    session = {
        "id": "one",
        "status": "launching",
        "sandbox_id": None,
        "notebook_url": None,
        "notebook_token": None,
        "gpu_key": "T4",
        "cpus": 4,
        "memory_gib": 32,
        "hourly_rate": 1.0,
        "max_runtime_seconds": 3600,
        "idle_timeout_minutes": 15,
        "started_at": now.isoformat(),
        "ended_at": None,
        "month_key": "2026-09",
        "reserved_usd": 5.0,
        "billed_usd": 0.0,
        "error": None,
    }
    allowed, _ = db.create_reservation(session=session, monthly_limit_usd=5.0)
    assert allowed
    duplicate = {**session, "id": "two"}
    allowed, message = db.create_reservation(session=duplicate, monthly_limit_usd=10.0)
    assert not allowed
    assert "already" in message
    db.update_session("one", status="stopped", reserved_usd=0.0, billed_usd=4.25)
    too_much = {**session, "id": "three", "reserved_usd": 0.76}
    allowed, message = db.create_reservation(session=too_much, monthly_limit_usd=5.0)
    assert not allowed
    assert "headroom" in message


def test_filename_sanitization_prevents_path_traversal():
    assert _safe_filename("../../unsafe name?.csv") == "unsafe name_.csv"
    assert _safe_filename("") == "dataset"



def test_launch_runtime_is_clamped_to_available_budget(tmp_path, monkeypatch):
    from notebook_studio.modal_provider import LaunchedSandbox, ModalProvider

    calls = []

    def fake_launch(self, **kwargs):
        calls.append(kwargs)
        return LaunchedSandbox("sb-test", "https://example.invalid/?token=test", "test")

    monkeypatch.setattr(ModalProvider, "launch", fake_launch)
    config = settings(
        tmp_path,
        modal_enabled=True,
        monthly_budget_usd=1.0,
        safety_buffer_usd=0.2,
    )
    app = create_app(config, Database(tmp_path / "budget-clamp.sqlite3"))
    client = TestClient(app)
    auth = base64.b64encode(b"me:test-password").decode()
    client.headers["Authorization"] = f"Basic {auth}"

    response = client.post(
        "/api/sessions",
        json={"gpu": "T4", "cpus": 4, "memory_gib": 32, "max_hours": 8},
    )
    assert response.status_code == 200
    session = response.json()["session"]
    assert 60 <= session["max_runtime_seconds"] < 8 * 3600
    assert session["reserved_usd"] <= 0.8
    assert calls[0]["runtime_seconds"] == session["max_runtime_seconds"]

    second = client.post(
        "/api/sessions",
        json={"gpu": "T4", "cpus": 4, "memory_gib": 32, "max_hours": 1},
    )
    assert second.status_code == 409
    assert "budget" in second.json()["detail"].lower()


def test_dataset_upload_records_safe_volume_path(tmp_path, monkeypatch):
    from notebook_studio.modal_provider import ModalProvider

    uploaded = []
    monkeypatch.setattr(ModalProvider, "upload", lambda self, src, dst, sandbox_id=None: uploaded.append((src, dst, sandbox_id)))
    client = client_for(tmp_path, modal_enabled=True)
    response = client.post(
        "/api/datasets",
        files={"file": ("../../my data?.csv", b"id,value\n1,2\n", "text/csv")},
    )
    assert response.status_code == 200
    data = response.json()["dataset"]
    assert data["name"] == "my data_.csv"
    assert data["path"].startswith("/datasets/")
    assert data["size_bytes"] == len(b"id,value\n1,2\n")
    assert uploaded[0][1] == "/workspace" + data["path"]
    assert uploaded[0][2] is None


def test_dashboard_serves_page_and_assets(tmp_path):
    client = client_for(tmp_path)
    page = client.get("/")
    assert page.status_code == 200
    assert "Choose your compute" in page.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/app.css").status_code == 200


def test_finished_sandbox_releases_budget_reservation(tmp_path, monkeypatch):
    from datetime import datetime, timedelta

    from notebook_studio.database import month_key
    from notebook_studio.modal_provider import ModalProvider

    now = datetime.now(timezone.utc)
    db = Database(tmp_path / "finished.sqlite3")
    session = {
        "id": "expired",
        "status": "running",
        "sandbox_id": "sb-expired",
        "notebook_url": "https://example.invalid/?token=secret",
        "notebook_token": "secret",
        "gpu_key": "T4",
        "cpus": 4,
        "memory_gib": 32,
        "hourly_rate": hourly_rate("T4", 4, 32),
        "max_runtime_seconds": 3600,
        "idle_timeout_minutes": 15,
        "started_at": (now - timedelta(minutes=10)).isoformat(),
        "ended_at": None,
        "month_key": month_key(now),
        "reserved_usd": 1.0,
        "billed_usd": 0.0,
        "error": None,
    }
    assert db.create_reservation(session=session, monthly_limit_usd=5.0)[0]
    monkeypatch.setattr(ModalProvider, "is_running", lambda self, sandbox_id: False)
    client = TestClient(create_app(settings(tmp_path, modal_enabled=True), db))
    auth = base64.b64encode(b"me:test-password").decode()
    client.headers["Authorization"] = f"Basic {auth}"

    response = client.get("/api/dashboard")
    assert response.status_code == 200
    finished = db.get_session("expired")
    assert finished["status"] == "finished"
    assert finished["reserved_usd"] == 0
    assert finished["billed_usd"] > 0
    assert response.json()["budget"]["active_session"] is None


def test_volume_path_mapping_uses_mount_root(tmp_path):
    from notebook_studio.modal_provider import ModalProvider

    assert ModalProvider._volume_path("/workspace/datasets/id/file.csv") == "/datasets/id/file.csv"
    try:
        ModalProvider._volume_path("/tmp/file.csv")
    except ValueError:
        pass
    else:
        raise AssertionError("outside workspace paths must be rejected")


def test_shared_accounts_have_isolated_sessions_datasets_and_budgets(tmp_path):
    from notebook_studio.credentials import password_hash

    db = Database(tmp_path / "tenants.sqlite3")
    db.create_user("alice", password_hash("alice-secret-password"))
    db.create_user("bob", password_hash("bob-secret-password"))
    now = datetime.now(timezone.utc)

    def session(session_id, owner, amount):
        return {
            "id": session_id,
            "owner_username": owner,
            "status": "running",
            "sandbox_id": "sb-" + session_id,
            "notebook_url": None,
            "notebook_token": None,
            "gpu_key": "T4",
            "cpus": 4,
            "memory_gib": 32,
            "hourly_rate": 1.0,
            "max_runtime_seconds": 3600,
            "idle_timeout_minutes": 15,
            "started_at": now.isoformat(),
            "ended_at": None,
            "month_key": now.strftime("%Y-%m"),
            "reserved_usd": amount,
            "billed_usd": 0.0,
            "error": None,
        }

    assert db.create_reservation(session=session("alice-session", "alice", 4), monthly_limit_usd=5)[0]
    assert db.create_reservation(session=session("bob-session", "bob", 4), monthly_limit_usd=5)[0]
    month = now.strftime("%Y-%m")
    assert db.monthly_usage(month, "alice")["reserved_usd"] == 4
    assert db.monthly_usage(month, "bob")["reserved_usd"] == 4
    assert [row["id"] for row in db.list_sessions(owner_username="alice")] == ["alice-session"]
    assert [row["id"] for row in db.list_sessions(owner_username="bob")] == ["bob-session"]

    db.add_dataset({"id": "d-a", "name": "a.csv", "path": "/datasets/a/a.csv", "size_bytes": 5, "uploaded_at": now.isoformat()}, "alice")
    assert len(db.list_datasets("alice")) == 1
    assert db.list_datasets("bob") == []
    assert db.get_dataset("d-a", "bob") is None


def test_shared_mode_authenticates_real_user_accounts(tmp_path):
    from notebook_studio.credentials import password_hash

    db = Database(tmp_path / "users.sqlite3")
    db.create_user("alice", password_hash("alice-secret-password"))
    config = settings(tmp_path, password="", multi_user_mode=True)
    app = create_app(config, db)
    client = TestClient(app)
    response = client.get("/api/dashboard", auth=("alice", "alice-secret-password"))
    assert response.status_code == 200
    assert response.json()["username"] == "alice"
    assert response.json()["budget"]["modal_enabled"] is False
    assert client.get("/api/dashboard", auth=("alice", "wrong-password")).status_code == 401
    assert client.get("/api/dashboard", auth=("bob", "alice-secret-password")).status_code == 401


def test_password_hash_uses_random_salt_and_verifies():
    from notebook_studio.credentials import password_hash, verify_password

    first = password_hash("a long example password")
    second = password_hash("a long example password")
    assert first != second
    assert verify_password("a long example password", first)
    assert not verify_password("wrong password", first)


def test_modal_volume_namespace_is_stable_and_account_scoped(monkeypatch):
    import sys
    from types import SimpleNamespace

    from notebook_studio.modal_provider import ModalProvider

    class FakeClient:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    clients = []

    class ClientFactory:
        @staticmethod
        def from_credentials(token_id, token_secret):
            assert token_id == "id-alice"
            assert token_secret == "secret-alice"
            client = FakeClient()
            clients.append(client)
            return client

    monkeypatch.setitem(sys.modules, "modal", SimpleNamespace(Client=ClientFactory))
    alice_a = ModalProvider("studio", "workspace", token_id="id-alice", token_secret="secret-alice", owner_username="alice")
    alice_b = ModalProvider("studio", "workspace", token_id="id-alice", token_secret="secret-alice", owner_username="alice")
    bob = ModalProvider("studio", "workspace", token_id="id-alice", token_secret="secret-alice", owner_username="bob")
    assert alice_a.volume_name == alice_b.volume_name
    assert alice_a.volume_name != bob.volume_name
    assert alice_a.client is clients[0]
    alice_a.close()
    assert clients[0].closed


def test_encrypted_modal_credentials_are_not_stored_as_plaintext():
    import pytest

    pytest.importorskip("cryptography")
    from notebook_studio.credentials import decrypt_modal_credentials, encrypt_modal_credentials

    encrypted_id, encrypted_secret = encrypt_modal_credentials(
        "a long random encryption key that is not the token", "modal-token-id", "modal-token-secret"
    )
    assert "modal-token-id" not in encrypted_id
    assert "modal-token-secret" not in encrypted_secret
    assert decrypt_modal_credentials(
        "a long random encryption key that is not the token", encrypted_id, encrypted_secret
    ) == ("modal-token-id", "modal-token-secret")




def test_modal_credentials_support_sdk_with_private_close_method(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace

    closed = []

    class FakeClient:
        def hello(self):
            return None

        def _close(self):
            closed.append(True)

    fake_modal = SimpleNamespace(Client=SimpleNamespace(
        from_credentials=lambda token_id, token_secret: FakeClient()
    ))
    monkeypatch.setitem(sys.modules, "modal", fake_modal)

    client = client_for(
        tmp_path,
        credential_encryption_key="test encryption key long enough for Fernet",
    )
    response = client.post(
        "/api/modal-credentials",
        json={"token_id": "modal-token-id", "token_secret": "modal-token-secret"},
    )

    assert response.status_code == 200
    assert response.json()["connected"] is True
    assert closed == [True]
    saved = client.get("/api/modal-credentials").json()
    assert saved["saved"] is True
