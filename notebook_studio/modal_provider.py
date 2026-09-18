from __future__ import annotations

import hashlib
import secrets
import shlex
import json
import time
from contextlib import suppress
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True)
class LaunchedSandbox:
    sandbox_id: str
    notebook_url: str
    token: str


def notebook_execution_script() -> str:
    # nbclient hooks make progress independent of CLI log formatting/version.
    return r'''import pathlib, sys
import nbformat
from nbclient import NotebookClient

notebook_path = pathlib.Path(sys.argv[1])
output_path = pathlib.Path(sys.argv[2])
timeout = int(sys.argv[3])
notebook = nbformat.read(notebook_path, as_version=4)
total = len(notebook.cells)

class StreamingNotebookClient(NotebookClient):
    async def on_cell_start(self, *, cell, cell_index):
        print(f"STUDIO_CELL_START:{cell_index + 1}/{total}", flush=True)

    async def on_cell_complete(self, *, cell, cell_index):
        print(f"STUDIO_CELL_COMPLETE:{cell_index + 1}/{total}", flush=True)
        for output in cell.get("outputs", []):
            if output.get("output_type") == "stream":
                text = output.get("text", "")
                print(text, end="" if text.endswith("\n") else "\n", flush=True)
            elif output.get("output_type") == "error":
                print("\n".join(output.get("traceback", [])), flush=True)

    async def on_cell_error(self, *, cell, cell_index, execute_reply):
        print(f"STUDIO_CELL_ERROR:{cell_index + 1}/{total}", flush=True)
        error = execute_reply.get("content", {})
        print(f"{error.get('ename', 'CellError')}: {error.get('evalue', '')}", flush=True)

client = StreamingNotebookClient(
    notebook,
    timeout=timeout,
    resources={"metadata": {"path": str(notebook_path.parent)}},
)
try:
    client.execute()
except Exception as exc:
    nbformat.write(notebook, output_path)
    print(f"STUDIO_NOTEBOOK_ERROR:{type(exc).__name__}: {exc}", flush=True)
    raise
nbformat.write(notebook, output_path)
'''


def close_modal_client(client) -> None:
    """Close an SDK client across Modal versions with differing close method names."""
    close = getattr(client, "close", None) or getattr(client, "_close", None)
    if close is not None:
        close()


class ModalProvider:
    """Thin SDK adapter. Imported and used only when MODAL_ENABLED=true."""

    def __init__(self, app_name: str, volume_name: str, *, token_id: str = "", token_secret: str = "", owner_username: str = "local"):
        try:
            import modal
        except ImportError as exc:
            raise RuntimeError(
                "Modal SDK is not installed. Install this project with the [modal] extra."
            ) from exc
        self.modal = modal
        if bool(token_id) != bool(token_secret):
            raise ValueError("Both Modal token ID and secret are required.")
        self.client = modal.Client.from_credentials(token_id, token_secret) if token_id else None
        namespace = hashlib.sha256(owner_username.encode("utf-8")).hexdigest()[:10]
        self.app_name = f"{app_name[:35]}-{namespace}"
        self.volume_name = f"{volume_name[:35]}-{namespace}"

    def close(self) -> None:
        if self.client is not None:
            close_modal_client(self.client)
            self.client = None

    def launch(
        self,
        *,
        gpu_key: str,
        cpus: int,
        memory_gib: int,
        runtime_seconds: int,
        idle_timeout_minutes: int,
    ) -> LaunchedSandbox:
        modal = self.modal
        app = modal.App.lookup(self.app_name, create_if_missing=True, client=self.client)
        volume = modal.Volume.from_name(self.volume_name, create_if_missing=True, client=self.client)
        image = (
            modal.Image.debian_slim(python_version="3.12")
            .uv_pip_install(
                "jupyterlab~=4.4",
                "ipykernel",
                "numpy",
                "pandas",
                "matplotlib",
                "scikit-learn",
                "torch",
                "transformers",
                "datasets",
                "accelerate",
                "safetensors",
            )
            .env(
                {
                    "HF_HOME": "/workspace/caches/huggingface",
                    "HF_HUB_CACHE": "/workspace/caches/huggingface/hub",
                    "TORCH_HOME": "/workspace/caches/torch",
                    "XDG_CACHE_HOME": "/workspace/caches/xdg",
                }
            )
        )
        token = secrets.token_urlsafe(32)
        port = 8888
        command = (
            "mkdir -p /workspace/notebooks /workspace/datasets /workspace/models "
            "/workspace/checkpoints /workspace/outputs /workspace/caches; "
            "exec jupyter lab --no-browser --allow-root --ip=0.0.0.0 "
            f"--port={port} --ServerApp.root_dir=/workspace "
            "--ServerApp.allow_remote_access=True "
            "--ServerApp.allow_origin='*' "
            '--IdentityProvider.token="$JUPYTER_TOKEN"'
        )
        with modal.enable_output():
            sandbox = modal.Sandbox.create(
                "bash",
                "-lc",
                command,
                app=app,
                image=image,
                env={"JUPYTER_TOKEN": token},
                gpu=gpu_key,
                cpu=(cpus, cpus),
                memory=(memory_gib * 1024, memory_gib * 1024),
                timeout=runtime_seconds,
                idle_timeout=idle_timeout_minutes * 60,
                encrypted_ports=[port],
                volumes={"/workspace": volume},
                client=self.client,
            )
        try:
            tunnel = sandbox.tunnels()[port]
            url = f"{tunnel.url}/?token={token}"
            self._wait_until_ready(tunnel.url, token, runtime_seconds=min(90, runtime_seconds))
            sandbox_id = sandbox.object_id
            sandbox.detach()
            return LaunchedSandbox(sandbox_id, url, token)
        except Exception:
            with suppress(Exception):
                sandbox.terminate(wait=True)
            with suppress(Exception):
                sandbox.detach()
            raise

    @staticmethod
    def _wait_until_ready(base_url: str, token: str, runtime_seconds: int) -> None:
        deadline = time.monotonic() + runtime_seconds
        status_url = f"{base_url}/api/status?token={token}"
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(status_url, timeout=4) as response:
                    if response.status == 200:
                        return
            except Exception as exc:
                last_error = exc
            time.sleep(2)
        raise RuntimeError(f"Jupyter did not become ready: {last_error or 'startup timeout'}")

    def terminate(self, sandbox_id: str) -> None:
        sandbox = self.modal.Sandbox.from_id(sandbox_id, client=self.client)
        try:
            sandbox.terminate(wait=True)
        finally:
            sandbox.detach()

    def is_running(self, sandbox_id: str) -> bool:
        # Keep the full poll result available to the local controller. The boolean
        # helper predates structured status and is still useful to callers that
        # only need to reconcile a reservation.
        return bool(self.sandbox_status(sandbox_id)["running"])

    def sandbox_status(self, sandbox_id: str) -> dict:
        sandbox = self.modal.Sandbox.from_id(sandbox_id, client=self.client)
        try:
            code = sandbox.poll()
            status = {"running": code is None, "exit_code": code}
            cache = getattr(self, "_last_sandbox_status", None)
            if cache is None:
                cache = self._last_sandbox_status = {}
            cache[sandbox_id] = status
            return status
        finally:
            sandbox.detach()

    def last_sandbox_status(self, sandbox_id: str) -> dict | None:
        """Return the result of the most recent is_running/sandbox_status poll."""
        return getattr(self, "_last_sandbox_status", {}).get(sandbox_id)

    def tail_logs(self, sandbox_id: str, entries: int = 100) -> list[dict]:
        sandbox = self.modal.Sandbox.from_id(sandbox_id, client=self.client)
        try:
            result = []
            for entry in sandbox.logs.tail(entries=max(1, min(entries, 500))):
                result.append({"timestamp": str(getattr(entry, "timestamp", "") or ""),
                               "source": str(getattr(entry, "source", "") or ""),
                               "message": str(getattr(entry, "message", entry))})
            return result
        finally:
            sandbox.detach()

    def resource_usage(self, sandbox_id: str) -> dict:
        sandbox = self.modal.Sandbox.from_id(sandbox_id, client=self.client)
        try:
            script = '''python3 - <<'PYMETRICS'
import json, re, subprocess
result = {"cpu_load_1m": None, "memory_total_bytes": None, "memory_available_bytes": None,
          "memory_source": None, "gpu": []}
try:
    result["cpu_load_1m"] = float(open("/proc/loadavg").read().split()[0])
except Exception:
    pass
try:
    values = {}
    for line in open("/proc/meminfo"):
        label, _, rest = line.partition(":")
        if label in {"MemTotal", "MemAvailable"}:
            values[label] = int(rest.split()[0]) * 1024
    result["memory_total_bytes"] = values.get("MemTotal")
    result["memory_available_bytes"] = values.get("MemAvailable")
    try:
        limit = open("/sys/fs/cgroup/memory.max").read().strip()
        current = int(open("/sys/fs/cgroup/memory.current").read().strip())
        if limit != "max":
            result["memory_total_bytes"] = int(limit)
            result["memory_available_bytes"] = max(0, int(limit) - current)
            result["memory_source"] = "cgroup"
    except Exception:
        result["memory_source"] = "proc"
except Exception:
    pass
try:
    text = subprocess.check_output(["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total", "--format=csv,noheader,nounits"], text=True, timeout=3)
    for line in text.splitlines():
        cells = [part.strip() for part in line.split(",")]
        if len(cells) == 4:
            result["gpu"].append({"name": cells[0], "utilization_percent": int(cells[1]), "memory_used_mib": int(cells[2]), "memory_total_mib": int(cells[3])})
except Exception:
    pass
print(json.dumps(result))
PYMETRICS'''
            process = sandbox.exec("bash", "-lc", script, timeout=10)
            output = process.stdout.read()
            if process.wait() != 0:
                raise RuntimeError("Could not read Sandbox resource metrics.")
            return json.loads(output.strip().splitlines()[-1])
        finally:
            sandbox.detach()

    def execute_notebook(self, sandbox_id: str, archive_path: str, notebook_path: str,
                         output_path: str, timeout_seconds: int, on_output) -> int:
        sandbox = self.modal.Sandbox.from_id(sandbox_id, client=self.client)
        archive = "/workspace" + archive_path
        out = "/workspace" + output_path
        work = out + "/work"
        extracted_notebook = work + "/" + notebook_path
        executed_notebook = out + "/executed.ipynb"
        extract = "import zipfile,sys; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])"
        executor = notebook_execution_script()
        command = (
            "set -euo pipefail; "
            f"mkdir -p {shlex.quote(work)} {shlex.quote(out)}; "
            f"python3 -c {shlex.quote(extract)} {shlex.quote(archive)} {shlex.quote(work)}; "
            f"python3 -c {shlex.quote(executor)} {shlex.quote(extracted_notebook)} "
            f"{shlex.quote(executed_notebook)} {max(1, int(timeout_seconds))}"
        )
        try:
            process = sandbox.exec("bash", "-lc", command, timeout=max(1, int(timeout_seconds)), pty=True)
            for chunk in process.stdout:
                on_output(str(chunk))
            return int(process.wait())
        finally:
            sandbox.detach()

    def list_volume_files(self, volume_path: str) -> list[dict]:
        volume = self.modal.Volume.from_name(self.volume_name, create_if_missing=True, client=self.client)
        root = self._volume_path("/workspace" + volume_path)
        try:
            entries = volume.listdir(root, recursive=True)
        except Exception as exc:
            if "not found" in str(exc).lower():
                return []
            raise
        output = []
        for entry in entries:
            path = str(getattr(entry, "path", "") or "")
            if not path.startswith("/"):
                path = "/" + path.lstrip("./")
            output.append({"path": path, "size_bytes": int(getattr(entry, "size", 0) or 0),
                           "type": str(getattr(entry, "type", "file")),
                           "modified_at": str(getattr(entry, "mtime", "") or "")})
        return output

    def download_volume_file(self, volume_path: str, destination: str) -> int:
        volume = self.modal.Volume.from_name(self.volume_name, create_if_missing=True, client=self.client)
        remote = self._volume_path("/workspace" + volume_path)
        total = 0
        with open(destination, "wb") as target:
            for chunk in volume.read_file(remote):
                target.write(chunk)
                total += len(chunk)
        return total

    @staticmethod
    def _volume_path(workspace_path: str) -> str:
        prefix = "/workspace/"
        if not workspace_path.startswith(prefix):
            raise ValueError("Workspace paths must stay below /workspace.")
        return "/" + workspace_path[len(prefix):]

    def upload(self, source_path: str, workspace_path: str, sandbox_id: str | None = None) -> None:
        if sandbox_id:
            sandbox = self.modal.Sandbox.from_id(sandbox_id, client=self.client)
            try:
                # Writing through the mounted Volume makes the file visible to this live JupyterLab.
                sandbox.filesystem.copy_from_local(source_path, workspace_path)
            finally:
                sandbox.detach()
            return
        volume = self.modal.Volume.from_name(self.volume_name, create_if_missing=True, client=self.client)
        with volume.batch_upload() as batch:
            batch.put_file(source_path, self._volume_path(workspace_path))

    def delete_workspace_path(self, workspace_path: str, sandbox_id: str | None = None) -> None:
        if sandbox_id:
            sandbox = self.modal.Sandbox.from_id(sandbox_id, client=self.client)
            try:
                sandbox.filesystem.remove(workspace_path)
            finally:
                sandbox.detach()
            return
        volume = self.modal.Volume.from_name(self.volume_name, create_if_missing=True, client=self.client)
        volume.remove_file(self._volume_path(workspace_path))
        volume.commit()
