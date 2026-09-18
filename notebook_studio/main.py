from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import math
import os
import re
import tempfile
import uuid
import zipfile
from pathlib import PurePosixPath
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from .config import Settings
from .credentials import USERNAME_RE, decrypt_modal_credentials, encrypt_modal_credentials, password_hash, verify_password
from .database import Database, iso_now, month_key, utc_now
from .pricing import (
    CPU_CHOICES,
    GPUS,
    GPU_BY_KEY,
    RAM_CHOICES_GIB,
    SANDBOX_CPU_PER_CORE_SECOND,
    SANDBOX_RAM_PER_GIB_SECOND,
    estimate_cost,
    hourly_rate,
)

PACKAGE_DIR = Path(__file__).resolve().parent
WEB_DIR = PACKAGE_DIR / "web"


class AdminCreateUser(BaseModel):
    username: str = Field(min_length=2, max_length=32)
    password: str = Field(min_length=12, max_length=1024)


class ModalCredentialsInput(BaseModel):
    token_id: str = Field(min_length=3, max_length=256)
    token_secret: str = Field(min_length=8, max_length=2048)


class StartSession(BaseModel):
    gpu: str
    cpus: int = Field(default=4)
    memory_gib: int = Field(default=32)
    max_hours: float = Field(default=2.0, gt=0, le=24)
    idle_timeout_minutes: int = Field(default=15, ge=1, le=1440)


class CreateKernel(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class StartKernelRun(BaseModel):
    version: int = Field(ge=1)
    timeout_seconds: int = Field(default=3600, ge=1, le=86400)
    kind: str = Field(default="benchmark", pattern="^benchmark$")


def load_local_env() -> None:
    """Read simple KEY=VALUE lines from .env without making dotenv a runtime dependency."""
    env_path = Path(".env")
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if not value or value.startswith("#") or "=" not in value:
            continue
        key, raw = value.split("=", 1)
        key, raw = key.strip(), raw.strip()
        if key and key not in os.environ:
            os.environ[key] = raw.strip('"').strip("'")


def _public_session(row: dict) -> dict:
    result = {
        key: row.get(key)
        for key in (
            "id",
            "status",
            "sandbox_id",
            "notebook_url",
            "gpu_key",
            "cpus",
            "memory_gib",
            "hourly_rate",
            "max_runtime_seconds",
            "idle_timeout_minutes",
            "started_at",
            "ended_at",
            "reserved_usd",
            "billed_usd",
            "error",
        )
    }
    match = re.fullmatch(r"Modal Sandbox exited with code (-?\d+)\.", row.get("error") or "")
    result["exit_code"] = int(match.group(1)) if match else None
    return result


def _safe_filename(filename: str) -> str:
    name = Path(filename or "dataset").name
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip(" .")
    return name[:160] or "dataset"


def _safe_archive_path(filename: str) -> str:
    normalized = (filename or "").replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise HTTPException(422, "File paths must be relative and cannot contain . or .. segments.")
    safe_parts = [re.sub(r"[^A-Za-z0-9._ -]", "_", part).strip(" .")[:120] for part in path.parts]
    if any(not part for part in safe_parts):
        raise HTTPException(422, "A file path contains an empty component.")
    return "/".join(safe_parts)


def _safe_volume_path(value: str, *, allow_root: bool = False) -> str:
    normalized = (value or "").replace("\\", "/").strip()
    parts = PurePosixPath(normalized).parts
    if normalized in {"", "/"} and allow_root:
        return "/"
    if any(part in {".", ".."} for part in parts):
        raise HTTPException(422, "Workspace paths cannot contain . or .. segments.")
    parts = tuple(part for part in parts if part not in {"/", ""})
    allowed = {"notebooks", "datasets", "models", "checkpoints", "outputs", "caches", "kernels"}
    if not parts or parts[0] not in allowed:
        raise HTTPException(422, "Workspace path must be inside notebooks, datasets, models, checkpoints, outputs, caches, or kernels.")
    return "/" + "/".join(parts)


def _public_run(run: dict, *, include_logs: bool = False) -> dict:
    fields = ("id", "kernel_id", "version", "kind", "status", "queue_position", "current_cell",
              "total_cells", "sandbox_id", "timeout_seconds", "started_at", "ended_at", "exit_code", "failure_reason",
              "output_path", "created_at")
    result = {key: run.get(key) for key in fields}
    result["events"] = run.get("events", [])
    if include_logs:
        result["logs"] = run.get("logs", "")
    return result


def _safe_output_relative_path(value: str) -> str:
    path = PurePosixPath((value or "").replace("\\", "/"))
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise HTTPException(422, "Output path must be relative and cannot contain . or .. segments.")
    return "/".join(path.parts)


def _seconds_until_month_end(moment: datetime) -> int:
    if moment.month == 12:
        next_month = moment.replace(year=moment.year + 1, month=1, day=1, hour=0, minute=0, second=0)
    else:
        next_month = moment.replace(month=moment.month + 1, day=1, hour=0, minute=0, second=0)
    return max(0, math.floor((next_month - moment).total_seconds()))


def create_app(settings: Settings | None = None, database: Database | None = None) -> FastAPI:
    config = settings or Settings.from_env()
    if config.app_public and not config.password and not config.multi_user_mode:
        raise ValueError("APP_PUBLIC=true requires APP_PASSWORD or MULTI_USER_MODE=true.")
    db = database or Database(config.database_path)
    app = FastAPI(title="Notebook Studio", docs_url=None, redoc_url=None)
    app.state.settings = config
    app.state.database = db
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

    @app.middleware("http")
    async def private_access(request: Request, call_next):
        if request.url.path == "/health":
            return await call_next(request)
        if request.method == "POST" and request.url.path == "/api/admin/users":
            supplied_admin_key = request.headers.get("x-admin-provisioning-key", "")
            if config.admin_provisioning_key and hmac.compare_digest(supplied_admin_key, config.admin_provisioning_key):
                request.state.username = "__admin__"
                return await call_next(request)
        auth = request.headers.get("authorization", "")
        try:
            scheme, encoded = auth.split(" ", 1)
            supplied = base64.b64decode(encoded, validate=True).decode("utf-8")
            username, password = supplied.split(":", 1)
        except (ValueError, UnicodeDecodeError):
            scheme, username, password = "", "", ""
        authenticated = False
        if scheme.lower() == "basic":
            if config.multi_user_mode:
                user = db.get_user(username)
                authenticated = bool(user and verify_password(password, user["password_hash"]))
            else:
                authenticated = bool(config.password and hmac.compare_digest(password, config.password))
                username = "local"
        if not authenticated:
            if not config.password and not config.multi_user_mode:
                detail, status = "Set APP_PASSWORD before opening Notebook Studio.", 503
            else:
                detail, status = "Authentication required. In shared mode, ask the administrator to provision your account.", 401
            headers = {"WWW-Authenticate": 'Basic realm="Notebook Studio"'} if status == 401 else {}
            return JSONResponse({"detail": detail}, status_code=status, headers=headers)
        request.state.username = username
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            host = request.headers.get("host")
            same_origin = f"{request.url.scheme}://{host}" if host else ""
            if origin and origin.rstrip("/") != same_origin and origin.rstrip("/") not in config.allowed_origins:
                return JSONResponse({"detail": "Cross-origin request rejected."}, status_code=403)
        return await call_next(request)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/")
    async def index():
        return FileResponse(WEB_DIR / "index.html")

    def public_session(row: dict | None) -> dict | None:
        if not row:
            return None
        safe = dict(row)
        notebook_url = safe.get("notebook_url")
        notebook_token = safe.get("notebook_token")
        if config.credential_encryption_key and notebook_url and notebook_token:
            if notebook_url.startswith("gAAAA") and notebook_token.startswith("gAAAA"):
                notebook_url, notebook_token = decrypt_modal_credentials(
                    config.credential_encryption_key, notebook_url, notebook_token
                )
            else:
                # Upgrade plaintext session links from earlier local versions on first read.
                encrypted_url, encrypted_token = encrypt_modal_credentials(
                    config.credential_encryption_key, notebook_url, notebook_token
                )
                db.update_session(safe["id"], notebook_url=encrypted_url, notebook_token=encrypted_token)
            safe["notebook_url"] = notebook_url
            safe["notebook_token"] = notebook_token
        return _public_session(safe)

    def modal_credentials(username: str) -> tuple[str, str] | None:
        stored = db.get_modal_credentials(username)
        if stored:
            if not config.credential_encryption_key:
                raise RuntimeError("MODAL_CREDENTIAL_ENCRYPTION_KEY is required to read saved credentials.")
            return decrypt_modal_credentials(
                config.credential_encryption_key,
                stored["token_id_ciphertext"],
                stored["token_secret_ciphertext"],
            )
        if not config.multi_user_mode and config.modal_enabled:
            token_id = os.getenv("MODAL_TOKEN_ID", "")
            token_secret = os.getenv("MODAL_TOKEN_SECRET", "")
            return (token_id, token_secret) if token_id and token_secret else ("", "")
        return None

    def make_provider(username: str):
        credentials = modal_credentials(username)
        if credentials is None:
            raise RuntimeError("Connect your own Modal API token in Account settings first.")
        token_id, token_secret = credentials
        if bool(token_id) != bool(token_secret):
            raise RuntimeError("Both MODAL_TOKEN_ID and MODAL_TOKEN_SECRET must be configured.")
        from .modal_provider import ModalProvider

        return ModalProvider(
            config.modal_app_name,
            config.modal_volume_name,
            token_id=token_id,
            token_secret=token_secret,
            owner_username=username,
        )

    def with_provider(username: str, operation):
        provider = make_provider(username)
        try:
            return operation(provider)
        finally:
            provider.close()


    def run_kernel_job(run_id: str, username: str) -> None:
        run = db.get_kernel_run(run_id, username)
        if not run:
            return
        version = db.get_kernel_version(run["kernel_id"], run["version"], username)
        session = next((row for row in db.list_sessions(30, username)
                        if row["status"] == "running" and row.get("sandbox_id")), None)
        if not version or version["status"] != "ready":
            db.update_kernel_run(run_id, status="failed", ended_at=iso_now(),
                                 failure_reason="Kernel version is missing or not ready.")
            db.append_kernel_run_log(run_id, "Kernel version is missing or not ready.\n",
                                     event={"type": "failed", "reason": "Kernel version is missing or not ready."})
            return
        if not session:
            message = "Start a Jupyter session before running a benchmark."
            db.update_kernel_run(run_id, status="failed", ended_at=iso_now(), failure_reason=message)
            db.append_kernel_run_log(run_id, message + "\n", event={"type": "failed", "reason": message})
            return
        started = iso_now()
        db.update_kernel_run(run_id, status="running", started_at=started, sandbox_id=session["sandbox_id"])
        db.append_kernel_run_log(run_id, f"Benchmark started for kernel {run['kernel_id']} version {run['version']}.\n",
                                 event={"type": "started", "kernel_id": run["kernel_id"], "version": run["version"]})
        def output(text: str) -> None:
            # ModalProvider emits stable markers from nbclient hooks. Keep
            # accepting nbconvert's older INFO log format for compatibility.
            markers = list(re.finditer(r"STUDIO_CELL_(START|COMPLETE|ERROR):(\d+)/(\d+)", text))
            legacy_match = re.search(r"Executing cell (\d+)", text) if not markers else None
            visible = re.sub(r"STUDIO_CELL_(?:START|COMPLETE|ERROR):\d+/\d+\s*", "", text)
            if visible:
                db.append_kernel_run_log(run_id, visible)
            if markers:
                for marker in markers:
                    phase, cell_text, total_text = marker.groups()
                    cell, total = int(cell_text), int(total_text)
                    db.update_kernel_run(run_id, current_cell=cell, total_cells=total)
                    db.append_kernel_run_log(run_id, "", event={
                        "type": {"START": "cell_started", "COMPLETE": "cell_completed", "ERROR": "cell_error"}[phase],
                        "current_cell": cell, "total_cells": total,
                    })
            elif legacy_match:
                cell = int(legacy_match.group(1)) + 1
                db.update_kernel_run(run_id, current_cell=cell, total_cells=version["cell_count"])
                db.append_kernel_run_log(run_id, "", event={"type": "cell_progress", "current_cell": cell,
                                                             "total_cells": version["cell_count"]})
        try:
            exit_code = with_provider(username, lambda provider: provider.execute_notebook(
                session["sandbox_id"], version["volume_path"], version["notebook_path"],
                run["output_path"], run["timeout_seconds"], output,
            ))
            status = "complete" if exit_code == 0 else "failed"
            reason = None if exit_code == 0 else f"Notebook process exited with code {exit_code}."
            finished_fields = {
                "status": status, "ended_at": iso_now(), "exit_code": exit_code,
                "total_cells": version["cell_count"], "failure_reason": reason,
            }
            if exit_code == 0:
                finished_fields["current_cell"] = version["cell_count"]
            # Preserve the last observed cell on errors so a terminal status still
            # identifies where execution stopped.
            db.update_kernel_run(run_id, **finished_fields)
            db.append_kernel_run_log(run_id, f"Benchmark {status}; exit code {exit_code}.\n",
                                     event={"type": status, "exit_code": exit_code, "failure_reason": reason})
        except Exception as exc:
            reason = str(exc)[:1000]
            db.update_kernel_run(run_id, status="failed", ended_at=iso_now(), failure_reason=reason)
            db.append_kernel_run_log(run_id, f"Benchmark failed: {reason}\n",
                                     event={"type": "failed", "reason": reason})

    async def reconcile_session(username: str) -> None:
        if not (db.get_modal_credentials(username) or (config.modal_enabled and not config.multi_user_mode)):
            return
        active = next(
            (row for row in db.list_sessions(30, username) if row["status"] == "running" and row["sandbox_id"]),
            None,
        )
        if not active:
            return
        def probe(provider):
            still_running = provider.is_running(active["sandbox_id"])
            getter = getattr(provider, "last_sandbox_status", None)
            observed = getter(active["sandbox_id"]) if callable(getter) else None
            return still_running, observed

        try:
            still_running, observed = await run_in_threadpool(
                with_provider, username, probe
            )
        except Exception:
            # A temporary API or account error must not release the reservation.
            return
        if not still_running:
            ended = utc_now()
            started = datetime.fromisoformat(active["started_at"])
            elapsed = min(active["max_runtime_seconds"], max(0, (ended - started).total_seconds()))
            charge = min(
                active["reserved_usd"],
                estimate_cost(active["gpu_key"], active["cpus"], active["memory_gib"], elapsed),
            )
            exit_code = (observed or {}).get("exit_code")
            failed = exit_code is not None and exit_code != 0
            reason = f"Modal Sandbox exited with code {exit_code}." if failed else None
            db.update_session(
                active["id"],
                status="failed" if failed else "finished",
                ended_at=ended.isoformat(timespec="seconds"),
                reserved_usd=0,
                billed_usd=charge,
                error=reason,
            )

    async def budget_payload(username: str) -> dict:
        await reconcile_session(username)
        current_month = month_key()
        usage = db.monthly_usage(current_month, username)
        limit = max(0.0, config.monthly_budget_usd - config.safety_buffer_usd)
        active = max(
            (row for row in db.list_sessions(30, username) if row["status"] in {"launching", "running", "stopping"}),
            key=lambda row: row.get("started_at") or "",
            default=None,
        )
        connected = bool(db.get_modal_credentials(username)) or (
            config.modal_enabled and not config.multi_user_mode
        )
        return {
            "month": current_month,
            "configured_monthly_budget_usd": config.monthly_budget_usd,
            "safety_buffer_usd": config.safety_buffer_usd,
            "app_limit_usd": limit,
            "spent_usd": usage["spent_usd"],
            "reserved_usd": usage["reserved_usd"],
            "available_usd": max(0.0, limit - usage["spent_usd"] - usage["reserved_usd"]),
            "modal_enabled": connected,
            "active_session": public_session(active),
        }

    @app.get("/api/dashboard")
    async def dashboard(request: Request):
        username = request.state.username
        gpu_options = []
        for gpu in GPUS:
            rate = hourly_rate(gpu.key, 4, 32)
            gpu_options.append(
                {
                    "key": gpu.key,
                    "name": gpu.name,
                    "vram_gib": gpu.vram_gib,
                    "gpu_usd_per_hour": round(gpu.usd_per_second * 3600, 6),
                    "hourly_rate_4cpu_32gib": round(rate, 4),
                    "note": gpu.note,
                }
            )
        return {
            "budget": await budget_payload(username),
            "gpus": gpu_options,
            "cpu_choices": CPU_CHOICES,
            "ram_choices_gib": RAM_CHOICES_GIB,
            "cpu_usd_per_core_hour": SANDBOX_CPU_PER_CORE_SECOND * 3600,
            "ram_usd_per_gib_hour": SANDBOX_RAM_PER_GIB_SECOND * 3600,
            "max_session_hours": config.max_session_hours,
            "default_idle_timeout_minutes": config.default_idle_timeout_minutes,
            "sessions": [public_session(row) for row in db.list_sessions(owner_username=username)],
            "datasets": db.list_datasets(username),
            "upload_limit_bytes": config.upload_limit_bytes,
            "workspace_volume_name": "Your personal Modal Volume",
            "modal_credentials_connected": bool(db.get_modal_credentials(username)) or (config.modal_enabled and not config.multi_user_mode),
            "modal_credentials_saved": bool(db.get_modal_credentials(username)),
            "username": username,
        }

    @app.post("/api/admin/users")
    async def provision_user(body: AdminCreateUser, request: Request):
        supplied_admin_key = request.headers.get("x-admin-provisioning-key", "")
        if not config.admin_provisioning_key or not hmac.compare_digest(
            supplied_admin_key, config.admin_provisioning_key
        ):
            raise HTTPException(404, "Not found.")
        if not USERNAME_RE.fullmatch(body.username):
            raise HTTPException(422, "Username must be 2-32 letters, numbers, dots, dashes, or underscores.")
        if db.get_user(body.username):
            raise HTTPException(409, "That username is already provisioned.")
        db.create_user(body.username, password_hash(body.password))
        return {"created": True, "username": body.username}

    @app.post("/api/modal-credentials")
    async def save_modal_credentials(body: ModalCredentialsInput, request: Request):
        if not config.credential_encryption_key:
            raise HTTPException(503, "The host has not configured MODAL_CREDENTIAL_ENCRYPTION_KEY.")
        if not config.multi_user_mode and request.state.username != "local":
            raise HTTPException(403, "This local instance only supports its configured Modal account.")
        try:
            from modal import Client
            from .modal_provider import close_modal_client
            client = await run_in_threadpool(Client.from_credentials, body.token_id, body.token_secret)
            try:
                await run_in_threadpool(client.hello)
            finally:
                await run_in_threadpool(close_modal_client, client)
        except ImportError as exc:
            raise HTTPException(503, "Install the Modal extra before connecting an account.") from exc
        except Exception as exc:
            raise HTTPException(422, "Modal rejected these credentials. Check the token ID and secret in your Modal dashboard.") from exc
        token_id_ciphertext, token_secret_ciphertext = encrypt_modal_credentials(
            config.credential_encryption_key, body.token_id, body.token_secret
        )
        db.set_modal_credentials(request.state.username, token_id_ciphertext, token_secret_ciphertext)
        return {"connected": True, "detail": "Modal credentials encrypted and saved for this account."}

    @app.get("/api/modal-credentials")
    async def get_modal_credential_status(request: Request):
        username = request.state.username
        connected = bool(db.get_modal_credentials(username)) or (
            config.modal_enabled and not config.multi_user_mode
        )
        return {"connected": connected, "saved": bool(db.get_modal_credentials(username)), "username": username}

    @app.delete("/api/modal-credentials")
    async def delete_modal_credentials(request: Request):
        active = next(
            (row for row in db.list_sessions(30, request.state.username) if row["status"] in {"launching", "running", "stopping"}),
            None,
        )
        if active:
            raise HTTPException(409, "Stop your active notebook before removing the saved Modal connection.")
        db.delete_modal_credentials(request.state.username)
        return {"connected": False, "detail": "Saved credentials were removed. Revoke the token in Modal too if needed."}

    @app.post("/api/sessions")
    async def launch_session(body: StartSession, request: Request):
        username = request.state.username
        if not (db.get_modal_credentials(username) or (config.modal_enabled and not config.multi_user_mode)):
            raise HTTPException(409, "Modal is disabled for this login. Connect your own Modal API token in Account settings before launching.")
        if body.gpu not in GPU_BY_KEY:
            raise HTTPException(422, "Choose a GPU from the available list.")
        if body.cpus not in CPU_CHOICES:
            raise HTTPException(422, "Unsupported CPU count.")
        if body.memory_gib not in RAM_CHOICES_GIB:
            raise HTTPException(422, "Unsupported RAM size.")
        if body.max_hours > config.max_session_hours:
            raise HTTPException(422, f"Maximum configured session is {config.max_session_hours:g} hours.")

        now = utc_now()
        max_seconds = min(
            math.floor(body.max_hours * 3600),
            math.floor(config.max_session_hours * 3600),
            _seconds_until_month_end(now),
        )
        if max_seconds < 60:
            raise HTTPException(409, "There is less than one minute left in this budget month.")
        rate = hourly_rate(body.gpu, body.cpus, body.memory_gib)
        monthly_limit = max(0.0, config.monthly_budget_usd - config.safety_buffer_usd)
        usage = db.monthly_usage(month_key(now), username)
        headroom = max(0.0, monthly_limit - usage["spent_usd"] - usage["reserved_usd"])
        max_seconds = min(max_seconds, math.floor(headroom / rate * 3600))
        if max_seconds < 60:
            raise HTTPException(409, "Available app budget cannot reserve at least one minute for this GPU.")
        reservation = estimate_cost(body.gpu, body.cpus, body.memory_gib, max_seconds)
        session_id = str(uuid.uuid4())
        row = {
            "id": session_id,
            "owner_username": username,
            "status": "launching",
            "sandbox_id": None,
            "notebook_url": None,
            "notebook_token": None,
            "gpu_key": body.gpu,
            "cpus": body.cpus,
            "memory_gib": body.memory_gib,
            "hourly_rate": rate,
            "max_runtime_seconds": max_seconds,
            "idle_timeout_minutes": body.idle_timeout_minutes,
            "started_at": now.isoformat(timespec="seconds"),
            "ended_at": None,
            "month_key": month_key(now),
            "reserved_usd": reservation,
            "billed_usd": 0.0,
            "error": None,
        }
        allowed, message = db.create_reservation(session=row, monthly_limit_usd=monthly_limit)
        if not allowed:
            raise HTTPException(409, message)

        try:
            launched = await run_in_threadpool(
                with_provider, username,
                lambda provider: provider.launch(
                    gpu_key=body.gpu,
                    cpus=body.cpus,
                    memory_gib=body.memory_gib,
                    runtime_seconds=max_seconds,
                    idle_timeout_minutes=body.idle_timeout_minutes,
                ),
            )
            notebook_url, notebook_token = launched.notebook_url, launched.token
            if config.credential_encryption_key:
                notebook_url, notebook_token = encrypt_modal_credentials(
                    config.credential_encryption_key, notebook_url, notebook_token
                )
            db.update_session(
                session_id,
                status="running",
                sandbox_id=launched.sandbox_id,
                notebook_url=notebook_url,
                notebook_token=notebook_token,
            )
        except Exception as exc:
            ended = utc_now()
            elapsed = max(0, (ended - now).total_seconds())
            charge = min(reservation, estimate_cost(body.gpu, body.cpus, body.memory_gib, elapsed))
            db.update_session(
                session_id,
                status="failed",
                ended_at=ended.isoformat(timespec="seconds"),
                reserved_usd=0,
                billed_usd=charge,
                error=str(exc)[:500],
            )
            raise HTTPException(502, f"Modal could not start the notebook: {exc}") from exc
        return {"session": public_session(db.get_session(session_id, username))}

    @app.post("/api/sessions/{session_id}/stop")
    async def stop_session(session_id: str, request: Request):
        username = request.state.username
        row = db.get_session(session_id, username)
        if not row:
            raise HTTPException(404, "Session not found.")
        if row["status"] not in {"launching", "running", "stopping"}:
            return {"session": public_session(row)}
        if not row["sandbox_id"]:
            db.update_session(
                session_id,
                status="failed",
                ended_at=iso_now(),
                reserved_usd=0,
                error="Launch was cancelled before a Sandbox ID was recorded.",
            )
            return {"session": public_session(db.get_session(session_id, username))}
        try:
            await run_in_threadpool(
                with_provider, username, lambda provider: provider.terminate(row["sandbox_id"])
            )
        except Exception as exc:
            raise HTTPException(502, f"Could not stop the Modal Sandbox: {exc}") from exc
        ended = utc_now()
        started = datetime.fromisoformat(row["started_at"])
        elapsed = min(row["max_runtime_seconds"], max(0, (ended - started).total_seconds()))
        charge = min(
            row["reserved_usd"],
            estimate_cost(row["gpu_key"], row["cpus"], row["memory_gib"], elapsed),
        )
        db.update_session(
            session_id,
            status="stopped",
            ended_at=ended.isoformat(timespec="seconds"),
            reserved_usd=0,
            billed_usd=charge,
        )
        return {"session": public_session(db.get_session(session_id, username))}

    @app.post("/api/datasets")
    async def upload_dataset(file: Annotated[UploadFile, File()], request: Request):
        username = request.state.username
        if not (db.get_modal_credentials(username) or (config.modal_enabled and not config.multi_user_mode)):
            raise HTTPException(409, "Connect your own Modal API token in Account settings first.")
        name = _safe_filename(file.filename or "dataset")
        dataset_id = str(uuid.uuid4())
        # Volume API paths are relative to the mount root, so /datasets maps to /workspace/datasets.
        volume_path = f"/datasets/{dataset_id}/{name}"
        size_bytes = 0
        try:
            with tempfile.NamedTemporaryFile(prefix="studio-upload-", delete=False) as temp:
                temporary_path = temp.name
                while chunk := await file.read(1024 * 1024):
                    size_bytes += len(chunk)
                    if size_bytes > config.upload_limit_bytes:
                        raise HTTPException(413, "File exceeds the configured upload limit.")
                    temp.write(chunk)
            if size_bytes == 0:
                raise HTTPException(400, "Empty files are not accepted.")
            active = next(
                (row for row in db.list_sessions(30, username) if row["status"] == "running" and row["sandbox_id"]),
                None,
            )
            workspace_path = "/workspace" + volume_path
            await run_in_threadpool(
                with_provider, username,
                lambda provider: provider.upload(
                    temporary_path, workspace_path, active["sandbox_id"] if active else None
                ),
            )
            dataset = {
                "id": dataset_id,
                "name": name,
                "path": volume_path,
                "size_bytes": size_bytes,
                "uploaded_at": iso_now(),
            }
            db.add_dataset(dataset, username)
            note = f"Uploaded {name} to persistent storage."
        finally:
            with suppress(UnboundLocalError, FileNotFoundError):
                os.unlink(temporary_path)
            await file.close()
        return {"dataset": dataset, "note": note}

    @app.delete("/api/datasets/{dataset_id}")
    async def delete_dataset(dataset_id: str, request: Request):
        username = request.state.username
        dataset = db.get_dataset(dataset_id, username)
        if not dataset:
            raise HTTPException(404, "Dataset not found.")
        if not (db.get_modal_credentials(username) or (config.modal_enabled and not config.multi_user_mode)):
            raise HTTPException(409, "Connect your own Modal API token in Account settings first.")
        try:
            active = next(
                (row for row in db.list_sessions(30, username) if row["status"] == "running" and row["sandbox_id"]),
                None,
            )
            await run_in_threadpool(
                with_provider, username,
                lambda provider: provider.delete_workspace_path(
                    "/workspace" + dataset["path"], active["sandbox_id"] if active else None
                ),
            )
        except Exception as exc:
            raise HTTPException(502, f"Could not remove the Volume file: {exc}") from exc
        db.remove_dataset(dataset_id, username)
        note = "Dataset deleted from persistent storage."
        return {"deleted": dataset_id, "note": note}

    @app.get("/api/sessions/{session_id}/status")
    async def session_status(session_id: str, request: Request):
        username = request.state.username
        row = db.get_session(session_id, username)
        if not row:
            raise HTTPException(404, "Session not found.")
        await reconcile_session(username)
        row = db.get_session(session_id, username) or row
        provider_status = None
        live_resources = None
        if row.get("sandbox_id") and row["status"] in {"launching", "running", "stopping"}:
            try:
                provider_status = await run_in_threadpool(
                    with_provider, username, lambda provider: provider.sandbox_status(row["sandbox_id"])
                )
                live_resources = await run_in_threadpool(
                    with_provider, username, lambda provider: provider.resource_usage(row["sandbox_id"])
                )
            except Exception as exc:
                provider_status = {"available": False, "error": str(exc)[:500]}
        elapsed = 0
        if row.get("started_at"):
            started = datetime.fromisoformat(row["started_at"])
            elapsed = max(0, int(((datetime.now(timezone.utc) if row["status"] in {"launching", "running", "stopping"}
                                   else datetime.fromisoformat(row.get("ended_at") or iso_now())) - started).total_seconds()))
        return {
            "session_id": session_id,
            "status": row["status"],
            "phase": "active" if row["status"] in {"launching", "running", "stopping"} else "terminal",
            "queue_position": None,
            "queue_position_detail": "The Sandbox API does not expose a queue position after the create call returns.",
            "worker_assignment": {"sandbox_id": row.get("sandbox_id"), "hostname": None,
                                  "detail": "Modal does not expose a worker hostname through this Sandbox client."},
            "progress": {"kind": "jupyter_session", "cell_progress": None,
                         "detail": "Jupyter kernel cell progress is available in /api/kernels/runs/{run_id} for managed benchmark runs."},
            "resources": {"requested": {"gpu": row["gpu_key"], "cpus": row["cpus"], "memory_gib": row["memory_gib"]},
                          "observed": live_resources},
            "elapsed_seconds": elapsed,
            "max_runtime_seconds": row["max_runtime_seconds"],
            "exit_code": (provider_status or {}).get("exit_code", _public_session(row).get("exit_code")),
            "failure_reason": row.get("error") or (provider_status or {}).get("error"),
            "started_at": row.get("started_at"), "ended_at": row.get("ended_at"),
        }

    @app.get("/api/sessions/{session_id}/logs")
    async def session_logs(session_id: str, request: Request, limit: int = 100):
        username = request.state.username
        row = db.get_session(session_id, username)
        if not row:
            raise HTTPException(404, "Session not found.")
        if not row.get("sandbox_id"):
            return {"session_id": session_id, "logs": [], "failure_reason": row.get("error")}
        try:
            logs = await run_in_threadpool(
                with_provider, username, lambda provider: provider.tail_logs(row["sandbox_id"], limit)
            )
        except Exception as exc:
            raise HTTPException(502, f"Could not read Modal logs: {exc}") from exc
        return {"session_id": session_id, "logs": logs, "failure_reason": row.get("error")}

    @app.get("/api/sessions/{session_id}/events")
    async def session_events(session_id: str, request: Request):
        username = request.state.username
        row = db.get_session(session_id, username)
        if not row:
            raise HTTPException(404, "Session not found.")
        async def stream():
            seen = set()
            last_status = None
            while not await request.is_disconnected():
                await reconcile_session(username)
                current = db.get_session(session_id, username)
                if not current:
                    break
                if current.get("sandbox_id"):
                    try:
                        logs = await run_in_threadpool(
                            with_provider, username, lambda provider: provider.tail_logs(current["sandbox_id"], 100)
                        )
                        for entry in logs:
                            key = (entry.get("timestamp"), entry.get("source"), entry.get("message"))
                            if key not in seen:
                                seen.add(key)
                                yield "event: log\ndata: " + json.dumps(entry) + "\n\n"
                    except Exception as exc:
                        yield "event: provider_error\ndata: " + json.dumps({"message": str(exc)[:500]}) + "\n\n"
                if current["status"] != last_status:
                    last_status = current["status"]
                    yield "event: status\ndata: " + json.dumps({"status": last_status, "failure_reason": current.get("error")}) + "\n\n"
                if current["status"] not in {"launching", "running", "stopping"}:
                    break
                yield ": keep-alive\n\n"
                await asyncio.sleep(1.5)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.get("/api/datasets/{dataset_id}/download")
    async def download_dataset(dataset_id: str, request: Request):
        username = request.state.username
        dataset = db.get_dataset(dataset_id, username)
        if not dataset:
            raise HTTPException(404, "Dataset not found.")
        try:
            fd, local_path = tempfile.mkstemp(prefix="studio-download-")
            os.close(fd)
            await run_in_threadpool(with_provider, username,
                                    lambda provider: provider.download_volume_file(dataset["path"], local_path))
        except Exception as exc:
            with suppress(UnboundLocalError, FileNotFoundError): os.unlink(local_path)
            raise HTTPException(502, f"Could not download dataset: {exc}") from exc
        return FileResponse(local_path, filename=dataset["name"], background=BackgroundTask(os.unlink, local_path))

    @app.get("/api/storage")
    async def list_storage(request: Request, path: str = "/"):
        username = request.state.username
        volume_path = _safe_volume_path(path, allow_root=True)
        if not (db.get_modal_credentials(username) or (config.modal_enabled and not config.multi_user_mode)):
            raise HTTPException(409, "Connect your own Modal API token in Account settings first.")
        try:
            files = await run_in_threadpool(with_provider, username,
                                            lambda provider: provider.list_volume_files(volume_path))
        except Exception as exc:
            raise HTTPException(502, f"Could not list persistent storage: {exc}") from exc
        return {"path": volume_path, "files": files}

    @app.post("/api/storage/files")
    async def upload_storage_file(request: Request, path: str = Form(...), file: UploadFile = File(...)):
        username = request.state.username
        directory = _safe_volume_path(path)
        filename = _safe_archive_path(file.filename or "file")
        if "/" in filename:
            raise HTTPException(422, "Use a simple filename for workspace uploads.")
        if directory == "/datasets":
            raise HTTPException(422, "Use the dataset upload action to keep the dataset list in sync.")
        target = directory.rstrip("/") + "/" + filename
        if not (db.get_modal_credentials(username) or (config.modal_enabled and not config.multi_user_mode)):
            raise HTTPException(409, "Connect your own Modal API token in Account settings first.")
        size = 0
        try:
            with tempfile.NamedTemporaryFile(prefix="studio-upload-", delete=False) as temp:
                local_path = temp.name
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > config.upload_limit_bytes:
                        raise HTTPException(413, "File exceeds the configured upload limit.")
                    temp.write(chunk)
            if size == 0: raise HTTPException(400, "Empty files are not accepted.")
            await run_in_threadpool(with_provider, username,
                                    lambda provider: provider.upload(local_path, "/workspace" + target))
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(502, f"Could not upload workspace file: {exc}") from exc
        finally:
            with suppress(UnboundLocalError, FileNotFoundError): os.unlink(local_path)
            await file.close()
        return {"file": {"path": target, "size_bytes": size}}

    @app.get("/api/storage/download")
    async def download_storage_file(request: Request, path: str):
        username = request.state.username
        volume_path = _safe_volume_path(path)
        try:
            fd, local_path = tempfile.mkstemp(prefix="studio-download-")
            os.close(fd)
            await run_in_threadpool(with_provider, username,
                                    lambda provider: provider.download_volume_file(volume_path, local_path))
        except Exception as exc:
            with suppress(UnboundLocalError, FileNotFoundError): os.unlink(local_path)
            raise HTTPException(502, f"Could not download workspace file: {exc}") from exc
        return FileResponse(local_path, filename=PurePosixPath(volume_path).name,
                            background=BackgroundTask(os.unlink, local_path))

    @app.delete("/api/storage/files")
    async def delete_storage_file(request: Request, path: str):
        username = request.state.username
        volume_path = _safe_volume_path(path)
        try:
            active = next((row for row in db.list_sessions(30, username)
                           if row["status"] == "running" and row.get("sandbox_id")), None)
            await run_in_threadpool(with_provider, username,
                                    lambda provider: provider.delete_workspace_path(
                                        "/workspace" + volume_path, active["sandbox_id"] if active else None))
        except Exception as exc:
            raise HTTPException(502, f"Could not delete workspace file: {exc}") from exc
        for dataset in db.list_datasets(username):
            if dataset["path"] == volume_path:
                db.remove_dataset(dataset["id"], username)
        return {"deleted": volume_path}

    @app.get("/api/kernels")
    async def list_kernels(request: Request):
        return {"kernels": db.list_kernels(request.state.username)}

    @app.post("/api/kernels")
    async def create_kernel(body: CreateKernel, request: Request):
        username = request.state.username
        kernel_id = str(uuid.uuid4())
        try:
            kernel = db.create_kernel(kernel_id, body.name.strip(), username)
        except Exception as exc:
            raise HTTPException(409, "A kernel with that name already exists for this account.") from exc
        return {"kernel": {key: kernel[key] for key in ("id", "name", "created_at")}, "latest_version": 0}

    @app.get("/api/kernels/{kernel_id}/versions")
    async def list_kernel_versions(kernel_id: str, request: Request):
        username = request.state.username
        if not db.get_kernel(kernel_id, username): raise HTTPException(404, "Kernel not found.")
        versions = db.list_kernel_versions(kernel_id, username)
        for version in versions: version["attachments"] = json.loads(version.pop("attachments_json", "[]"))
        return {"kernel_id": kernel_id, "versions": versions}

    @app.post("/api/kernels/{kernel_id}/versions")
    async def push_kernel_version(kernel_id: str, request: Request,
                                  notebook: UploadFile = File(...),
                                  attachments: list[UploadFile] = File(default=[])):
        username = request.state.username
        if not db.get_kernel(kernel_id, username): raise HTTPException(404, "Kernel not found.")
        if not (db.get_modal_credentials(username) or (config.modal_enabled and not config.multi_user_mode)):
            raise HTTPException(409, "Connect your own Modal API token in Account settings first.")
        staged = []
        total_size = 0
        notebook_path = _safe_archive_path(notebook.filename or "kernel.ipynb")
        if not notebook_path.lower().endswith(".ipynb"):
            raise HTTPException(422, "The notebook filename must end in .ipynb.")
        try:
            with tempfile.TemporaryDirectory(prefix="studio-kernel-version-") as directory:
                root = Path(directory)
                seen = set()
                for item in [notebook, *attachments]:
                    name = _safe_archive_path(item.filename or "attachment")
                    if name in seen: raise HTTPException(422, f"Duplicate bundle path: {name}")
                    seen.add(name)
                    local = root / name
                    local.parent.mkdir(parents=True, exist_ok=True)
                    size = 0
                    with local.open("wb") as target:
                        while chunk := await item.read(1024 * 1024):
                            size += len(chunk); total_size += len(chunk)
                            if total_size > config.upload_limit_bytes:
                                raise HTTPException(413, "Kernel bundle exceeds the configured upload limit.")
                            target.write(chunk)
                    if size == 0 and item is notebook:
                        raise HTTPException(400, "The notebook file is empty.")
                    staged.append((name, local))
                try:
                    notebook_json = json.loads((root / notebook_path).read_text(encoding="utf-8"))
                    if not isinstance(notebook_json.get("cells"), list): raise ValueError("missing cells")
                except Exception as exc:
                    raise HTTPException(422, "The notebook is not valid Jupyter .ipynb JSON.") from exc
                bundle_path = root / "bundle.zip"
                with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    for name, local in staged: archive.write(local, name)
                    archive.writestr("manifest.json", json.dumps({"notebook_path": notebook_path,
                                                                   "files": [name for name, _ in staged]}, sort_keys=True))
                bundle_size = bundle_path.stat().st_size
                digest_state = hashlib.sha256()
                with bundle_path.open("rb") as bundle_file:
                    for chunk in iter(lambda: bundle_file.read(1024 * 1024), b""):
                        digest_state.update(chunk)
                digest = digest_state.hexdigest()
                version_id = str(uuid.uuid4())
                # Allocate the ordinal transactionally before upload; failed uploads remain visible as failed versions.
                placeholder = db.create_kernel_version({
                    "id": version_id, "kernel_id": kernel_id, "status": "uploading",
                    "notebook_path": notebook_path,
                    "attachments_json": json.dumps([name for name, _ in staged if name != notebook_path]),
                    "volume_path": f"/kernels/{kernel_id}/versions/pending/bundle.zip",
                    "sha256": digest, "size_bytes": bundle_size, "file_count": len(staged),
                    "cell_count": len(notebook_json["cells"]), "created_at": iso_now(),
                }, username)
                volume_path = f"/kernels/{kernel_id}/versions/{placeholder['version']}/bundle.zip"
                db.update_kernel_version(version_id, volume_path=volume_path)
                try:
                    await run_in_threadpool(with_provider, username,
                                            lambda provider: provider.upload(str(bundle_path), "/workspace" + volume_path))
                except Exception as exc:
                    db.update_kernel_version(version_id, status="failed")
                    raise HTTPException(502, f"Could not store immutable kernel version: {exc}") from exc
                db.update_kernel_version(version_id, status="ready")
                record = db.get_kernel_version(kernel_id, placeholder["version"], username)
                record["attachments"] = json.loads(record.pop("attachments_json", "[]"))
                return {"kernel_id": kernel_id, "version": record}
        finally:
            for item in [notebook, *attachments]: await item.close()

    @app.post("/api/kernels/{kernel_id}/runs")
    async def start_kernel_run(kernel_id: str, body: StartKernelRun, request: Request,
                               background_tasks: BackgroundTasks):
        username = request.state.username
        if not db.get_kernel(kernel_id, username): raise HTTPException(404, "Kernel not found.")
        version = db.get_kernel_version(kernel_id, body.version, username)
        if not version or version["status"] != "ready": raise HTTPException(404, "Ready kernel version not found.")
        active = next((row for row in db.list_sessions(30, username)
                       if row["status"] == "running" and row.get("sandbox_id")), None)
        if not active: raise HTTPException(409, "Start a Jupyter GPU session before running a benchmark.")
        started = datetime.fromisoformat(active["started_at"])
        remaining_seconds = max(0, active["max_runtime_seconds"] - int((utc_now() - started).total_seconds()))
        timeout_seconds = min(body.timeout_seconds, remaining_seconds)
        if timeout_seconds < 1:
            raise HTTPException(409, "The active session has reached its runtime limit; start a new session before benchmarking.")
        run_id = str(uuid.uuid4())
        run = db.create_kernel_run({"id": run_id, "owner_username": username, "kernel_id": kernel_id,
                                    "version": body.version, "kind": body.kind,
                                    "timeout_seconds": timeout_seconds,
                                    "output_path": f"/outputs/{run_id}", "created_at": iso_now(),
                                    "total_cells": version["cell_count"]})
        db.append_kernel_run_log(run_id, "Benchmark queued for the active Modal Sandbox.\n",
                                 event={"type": "queued", "queue_position": None})
        background_tasks.add_task(run_kernel_job, run_id, username)
        return {"run": _public_run(db.get_kernel_run(run_id, username) or run)}

    @app.get("/api/kernels/{kernel_id}/runs")
    async def list_kernel_runs(kernel_id: str, request: Request):
        username = request.state.username
        if not db.get_kernel(kernel_id, username): raise HTTPException(404, "Kernel not found.")
        return {"kernel_id": kernel_id, "runs": [_public_run(run) for run in db.list_kernel_runs(kernel_id, username)]}

    @app.get("/api/kernels/runs/{run_id}")
    async def get_kernel_run(run_id: str, request: Request):
        run = db.get_kernel_run(run_id, request.state.username)
        if not run: raise HTTPException(404, "Benchmark run not found.")
        session = db.get_session_by_sandbox(run.get("sandbox_id"), request.state.username) if run.get("sandbox_id") else None
        observed = None
        if session and session["status"] in {"launching", "running", "stopping"}:
            try:
                observed = await run_in_threadpool(
                    with_provider, request.state.username,
                    lambda provider: provider.resource_usage(session["sandbox_id"]),
                )
            except Exception as exc:
                observed = {"available": False, "error": str(exc)[:500]}
        return {**_public_run(run, include_logs=True),
                "worker_assignment": {"sandbox_id": run.get("sandbox_id"), "hostname": None,
                                      "detail": "Modal does not expose a worker hostname through this Sandbox client."},
                "cell_progress": {"current": run.get("current_cell"), "total": run.get("total_cells")},
                "resources": {"requested": ({"gpu": session["gpu_key"], "cpus": session["cpus"],
                                               "memory_gib": session["memory_gib"]} if session else None),
                              "observed": observed},
                "queue_position_detail": "The benchmark executes directly inside an already-running Sandbox; Modal does not expose a queue position.",
                "session_id": session["id"] if session else None}

    @app.get("/api/kernels/runs/{run_id}/events")
    async def kernel_run_events(run_id: str, request: Request):
        username = request.state.username
        if not db.get_kernel_run(run_id, username): raise HTTPException(404, "Benchmark run not found.")
        async def stream():
            offset = 0
            event_offset = 0
            while not await request.is_disconnected():
                run = db.get_kernel_run(run_id, username)
                if not run: break
                logs = run.get("logs", "")
                if len(logs) > offset:
                    yield "event: log\ndata: " + json.dumps({"text": logs[offset:]}) + "\n\n"
                    offset = len(logs)
                events = run.get("events", [])
                for event in events[event_offset:]:
                    yield "event: progress\ndata: " + json.dumps(event) + "\n\n"
                event_offset = len(events)
                yield "event: status\ndata: " + json.dumps(_public_run(run)) + "\n\n"
                if run["status"] not in {"queued", "running"}: break
                await asyncio.sleep(0.5)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.get("/api/kernels/runs/{run_id}/outputs")
    async def list_kernel_outputs(run_id: str, request: Request):
        username = request.state.username
        run = db.get_kernel_run(run_id, username)
        if not run: raise HTTPException(404, "Benchmark run not found.")
        try:
            files = await run_in_threadpool(with_provider, username,
                                            lambda provider: provider.list_volume_files(run["output_path"]))
        except Exception as exc:
            raise HTTPException(502, f"Could not list run outputs: {exc}") from exc
        return {"run_id": run_id, "files": files}

    @app.get("/api/kernels/runs/{run_id}/outputs/download")
    async def download_kernel_output(run_id: str, request: Request, path: str):
        username = request.state.username
        run = db.get_kernel_run(run_id, username)
        if not run: raise HTTPException(404, "Benchmark run not found.")
        relative = _safe_output_relative_path(path)
        try:
            fd, local_path = tempfile.mkstemp(prefix="studio-output-")
            os.close(fd)
            volume_path = run["output_path"].rstrip("/") + "/" + relative
            await run_in_threadpool(with_provider, username,
                                    lambda provider: provider.download_volume_file(volume_path, local_path))
        except Exception as exc:
            with suppress(UnboundLocalError, FileNotFoundError): os.unlink(local_path)
            raise HTTPException(502, f"Could not download run output: {exc}") from exc
        return FileResponse(local_path, filename=PurePosixPath(relative).name,
                            background=BackgroundTask(os.unlink, local_path))

    @app.get("/api/kernels/{kernel_id}/versions/{version}/download")
    async def download_kernel_version(kernel_id: str, version: int, request: Request):
        username = request.state.username
        record = db.get_kernel_version(kernel_id, version, username)
        if not record or record["status"] != "ready": raise HTTPException(404, "Ready kernel version not found.")
        try:
            fd, local_path = tempfile.mkstemp(prefix="studio-version-")
            os.close(fd)
            await run_in_threadpool(with_provider, username,
                                    lambda provider: provider.download_volume_file(record["volume_path"], local_path))
        except Exception as exc:
            with suppress(UnboundLocalError, FileNotFoundError): os.unlink(local_path)
            raise HTTPException(502, f"Could not download kernel version: {exc}") from exc
        return FileResponse(local_path, filename=f"{kernel_id}-v{version}.zip",
                            background=BackgroundTask(os.unlink, local_path))

    @app.get("/api/usage")
    async def usage(request: Request):
        budget = await budget_payload(request.state.username)
        limit = budget["app_limit_usd"]
        return {
            **budget,
            "utilization": min(
                1.0,
                (budget["spent_usd"] + budget["reserved_usd"]) / limit if limit else 0.0,
            ),
            "notice": (
                "Estimate for sessions launched here. Modal billing and other apps in the "
                "workspace can differ; check the Modal dashboard for authoritative usage."
            ),
        }

    # The public Worker hosts static files only. It may call this per-user local
    # helper from the browser; allow only explicitly configured origins.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(config.allowed_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Admin-Provisioning-Key"],
        max_age=600,
    )
    return app


load_local_env()
app = create_app()
