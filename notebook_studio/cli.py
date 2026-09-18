from __future__ import annotations

import argparse
import base64
import getpass
import json
import mimetypes
import pprint
import os
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any
from urllib.parse import quote

try:
    import httpx
except ImportError:  # pragma: no cover - gives a useful install hint
    httpx = None


class StudioError(RuntimeError):
    pass


def load_cli_env() -> None:
    """Load the simple local .env file without pulling in the web application."""
    env_path = Path(".env")
    if not env_path.is_file():
        return
    values = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"").strip("'")
    mapping = {"APP_PASSWORD": "NOTEBOOK_STUDIO_PASSWORD", "APP_USERNAME": "NOTEBOOK_STUDIO_USERNAME",
               "APP_URL": "NOTEBOOK_STUDIO_URL"}
    for source, target in mapping.items():
        if target not in os.environ and values.get(source):
            os.environ[target] = values[source]


class StudioClient:
    """Small authenticated API client shared by the CLI and MCP server."""

    def __init__(self, base_url: str, username: str, password: str, *, timeout: float = 60, transport=None):
        if httpx is None:
            raise StudioError("Install Notebook Studio with its standard dependencies (httpx is required).")
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(base_url=self.base_url, auth=(username, password), timeout=timeout,
                                    follow_redirects=True, transport=transport)

    @classmethod
    def from_env(cls, *, prompt: bool = True) -> "StudioClient":
        load_cli_env()
        username = os.getenv("NOTEBOOK_STUDIO_USERNAME", "local")
        password = os.getenv("NOTEBOOK_STUDIO_PASSWORD", "")
        if not password and prompt and sys.stdin.isatty():
            password = getpass.getpass("Notebook Studio password: ")
        if not password:
            raise StudioError("Set NOTEBOOK_STUDIO_PASSWORD or run interactively to enter it.")
        return cls(os.getenv("NOTEBOOK_STUDIO_URL", "http://127.0.0.1:8000"), username, password)

    def close(self) -> None:
        self._client.close()

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.RequestError as exc:
            raise StudioError(f"Cannot reach Notebook Studio at {self.base_url}: {exc}") from exc
        if not response.is_success:
            try:
                detail = response.json().get("detail", response.text)
            except Exception:
                detail = response.text
            raise StudioError(f"HTTP {response.status_code}: {detail}")
        if response.status_code == 204:
            return None
        if "application/json" in response.headers.get("content-type", ""):
            return response.json()
        return response

    def json(self, method: str, path: str, data: Any | None = None, **kwargs: Any) -> Any:
        return self.request(method, path, json=data, **kwargs)

    def upload(self, path: str, *, fields: dict[str, str] | None = None,
               files: list[tuple[str, tuple[str, Any, str]]] | None = None) -> Any:
        return self.request("POST", path, data=fields or {}, files=files or [])

    def download(self, path: str, destination: str | Path, **kwargs: Any) -> dict[str, Any]:
        output = Path(destination).expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
        partial = output.with_name(output.name + ".part")
        total = 0
        try:
            with self._client.stream("GET", path, **kwargs) as response:
                if not response.is_success:
                    response.read()
                    try: detail = response.json().get("detail", response.text)
                    except Exception: detail = response.text
                    raise StudioError(f"HTTP {response.status_code}: {detail}")
                with partial.open("wb") as target:
                    for chunk in response.iter_bytes(1024 * 1024):
                        target.write(chunk)
                        total += len(chunk)
            partial.replace(output)
        except httpx.RequestError as exc:
            partial.unlink(missing_ok=True)
            raise StudioError(f"Download failed: {exc}") from exc
        except Exception:
            partial.unlink(missing_ok=True)
            raise
        return {"saved_to": str(output.resolve()), "size_bytes": total}

    def events(self, path: str, *, stop_when_complete: bool = True) -> None:
        if httpx is None:
            raise StudioError("httpx is required for live event streaming.")
        try:
            with self._client.stream("GET", path, headers={"Accept": "text/event-stream"}, timeout=None) as response:
                if not response.is_success:
                    response.read()
                    raise StudioError(f"HTTP {response.status_code}: {response.text[:1000]}")
                event = "message"
                for line in response.iter_lines():
                    if line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:"):
                        data = line[5:].strip()
                        try:
                            payload = json.loads(data)
                        except json.JSONDecodeError:
                            payload = data
                        print(json.dumps({"event": event, "data": payload}, ensure_ascii=False), flush=True)
                        if stop_when_complete and event == "status" and isinstance(payload, dict) \
                                and payload.get("status") in {"complete", "failed", "stopped", "finished"}:
                            return
        except KeyboardInterrupt:
            return
        except httpx.RequestError as exc:
            raise StudioError(f"Live event stream failed: {exc}") from exc


    def collect_events(self, path: str, *, max_wait_seconds: int = 20) -> dict[str, Any]:
        """Collect a bounded slice of an SSE stream for MCP callers."""
        if httpx is None:
            raise StudioError("httpx is required for live event streaming.")
        wait_seconds = max(1, min(60, int(max_wait_seconds)))
        deadline = time.monotonic() + wait_seconds
        events: list[dict[str, Any]] = []
        complete = False
        timed_out = False
        event_name = "message"
        data_lines: list[str] = []

        def flush_event() -> bool:
            nonlocal event_name, data_lines, complete
            if not data_lines:
                event_name = "message"
                return False
            raw = "\n".join(data_lines)
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = raw
            events.append({"event": event_name, "data": payload})
            terminal = (
                event_name == "status"
                and isinstance(payload, dict)
                and payload.get("status") not in {"queued", "launching", "running", "stopping"}
            )
            event_name = "message"
            data_lines = []
            if terminal:
                complete = True
            return terminal

        try:
            with self._client.stream(
                "GET", path, headers={"Accept": "text/event-stream"}, timeout=wait_seconds + 2
            ) as response:
                if not response.is_success:
                    response.read()
                    raise StudioError(f"HTTP {response.status_code}: {response.text[:1000]}")
                for line in response.iter_lines():
                    if time.monotonic() >= deadline or len(events) >= 1000:
                        timed_out = True
                        break
                    if not line:
                        if flush_event():
                            break
                    elif line.startswith(":"):
                        continue
                    elif line.startswith("event:"):
                        event_name = line[6:].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
                else:
                    flush_event()
        except httpx.RequestError as exc:
            raise StudioError(f"Live event stream failed: {exc}") from exc
        return {
            "events": events,
            "complete": complete,
            "timed_out": timed_out,
            "max_wait_seconds": wait_seconds,
        }


def redact_notebook_links(value: Any) -> Any:
    """Remove bearer-bearing notebook links from status data before CLI/MCP output."""
    if isinstance(value, dict):
        return {
            key: redact_notebook_links(item)
            for key, item in value.items()
            if key not in {"notebook_url", "notebook_token"}
        }
    if isinstance(value, list):
        return [redact_notebook_links(item) for item in value]
    return value


def open_session(client: StudioClient, session_id: str) -> dict[str, Any]:
    dashboard = client.json("GET", "/api/dashboard")
    session = next((item for item in dashboard["sessions"] if item["id"] == session_id), None)
    if session is None:
        raise StudioError("Session not found.")
    if session["status"] != "running":
        raise StudioError(f"Session is {session['status']}; JupyterLab is not ready to open.")
    url = session.get("notebook_url")
    if not url:
        raise StudioError("This session has no JupyterLab URL yet. Check its status and try again.")
    try:
        opened = webbrowser.open(url, new=2)
    except Exception as exc:
        raise StudioError(f"Could not open a browser ({type(exc).__name__}).") from exc
    if not opened:
        raise StudioError("No browser accepted the notebook link. Set BROWSER or open JupyterLab from the website.")
    return {"session_id": session_id, "status": session["status"], "opened": True}


def attachment_archive_name(notebook_path: str | Path, attachment_path: str | Path) -> str:
    notebook_parent = Path(notebook_path).expanduser().resolve().parent
    attachment = Path(attachment_path).expanduser().resolve()
    try:
        return attachment.relative_to(notebook_parent).as_posix()
    except ValueError:
        return attachment.name


def _file_part(path: str | Path, *, upload_name: str | None = None):
    local = Path(path).expanduser()
    if not local.is_file():
        raise StudioError(f"File does not exist: {local}")
    mime = mimetypes.guess_type(local.name)[0] or "application/octet-stream"
    return (upload_name or local.name, local.open("rb"), mime)


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="notebook-studio", description="AI-first CLI for your Notebook Studio workspace.")
    parser.add_argument("--url", default=os.getenv("NOTEBOOK_STUDIO_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--username", default=os.getenv("NOTEBOOK_STUDIO_USERNAME", "local"))
    parser.add_argument("--password", default=os.getenv("NOTEBOOK_STUDIO_PASSWORD"), help=argparse.SUPPRESS)
    parser.add_argument("--format", choices=("json", "text"), default="json", help="Output format (default: json).")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("status", help="Show dashboard, active session, GPU choices, datasets, and connection state.")
    commands.add_parser("gpus", help="List available GPU, VRAM, resource, and estimated-rate options.")
    estimate = commands.add_parser("estimate", help="Estimate a session's rate and budget-capped runtime.")
    estimate.add_argument("--gpu", required=True)
    estimate.add_argument("--cpus", type=int, default=4)
    estimate.add_argument("--ram", type=int, default=32, help="Sandbox RAM in GiB.")
    estimate.add_argument("--hours", type=float, default=2)
    commands.add_parser("usage", help="Show app-side monthly budget and recent session history.")

    sessions = commands.add_parser("sessions", help="Manage notebook sessions.").add_subparsers(dest="action", required=True)
    sessions.add_parser("list", help="List sessions and statuses.")
    start = sessions.add_parser("start", help="Start JupyterLab on the chosen Modal GPU.")
    start.add_argument("--gpu", required=True)
    start.add_argument("--cpus", type=int, default=4)
    start.add_argument("--ram", type=int, default=32, help="Sandbox RAM in GiB.")
    start.add_argument("--hours", type=float, default=2)
    start.add_argument("--idle-minutes", type=int, default=15)
    stop = sessions.add_parser("stop", help="Stop a session and release its reservation.")
    stop.add_argument("session_id")
    detail = sessions.add_parser("status", help="Show detailed status, assignment, resources, progress, and failure reason.")
    detail.add_argument("session_id")
    open_session = sessions.add_parser("open", help="Open a running session's JupyterLab in your browser.")
    open_session.add_argument("session_id")
    logs = sessions.add_parser("logs", help="Read or follow session logs/events.")
    logs.add_argument("session_id")
    logs.add_argument("--follow", action="store_true")

    datasets = commands.add_parser("datasets", help="Manage persistent dataset files.").add_subparsers(dest="action", required=True)
    datasets.add_parser("list", help="List uploaded datasets.")
    upload = datasets.add_parser("upload", help="Upload a dataset to your Modal Volume.")
    upload.add_argument("file")
    delete = datasets.add_parser("delete", help="Delete a dataset from your Modal Volume.")
    delete.add_argument("dataset_id")
    download = datasets.add_parser("download", help="Download a dataset from your Modal Volume.")
    download.add_argument("dataset_id")
    download.add_argument("--output", required=True)

    storage = commands.add_parser("storage", help="Browse and manage your persistent workspace files.").add_subparsers(dest="action", required=True)
    listing = storage.add_parser("list"); listing.add_argument("--path", default="/")
    put = storage.add_parser("upload"); put.add_argument("file"); put.add_argument("--path", required=True, help="Workspace directory, e.g. /models")
    get = storage.add_parser("download"); get.add_argument("path"); get.add_argument("--output", required=True)
    rm = storage.add_parser("delete"); rm.add_argument("path")

    account = commands.add_parser("account", help="Connect, inspect, or forget the Modal credential saved for this login.").add_subparsers(dest="action", required=True)
    account.add_parser("status")
    connect = account.add_parser("connect", help="Verify and save an encrypted Modal token.")
    connect.add_argument("--token-id")
    connect.add_argument("--token-secret")
    account.add_parser("forget")

    users = commands.add_parser("users", help="Shared-site account administration.").add_subparsers(dest="action", required=True)
    provision = users.add_parser("provision"); provision.add_argument("username"); provision.add_argument("--password")

    kernel = commands.add_parser("kernel", help="Versioned notebook bundles and separate benchmark runs.").add_subparsers(dest="action", required=True)
    kernel.add_parser("list", help="List stable kernel IDs and latest immutable version numbers.")
    create = kernel.add_parser("create"); create.add_argument("name")
    push = kernel.add_parser("push", help="Publish a new immutable version to a stable kernel ID; include files as attachments.")
    push.add_argument("notebook"); push.add_argument("--kernel-id"); push.add_argument("--name")
    push.add_argument("--attachment", action="append", default=[])
    versions = kernel.add_parser("versions"); versions.add_argument("kernel_id")
    run = kernel.add_parser("benchmark", help="Run one immutable version in the currently active GPU session.")
    run.add_argument("kernel_id"); run.add_argument("--version", type=int, required=True)
    run.add_argument("--timeout", type=int, default=3600); run.add_argument("--follow", action="store_true")
    runs = kernel.add_parser("runs"); runs.add_argument("kernel_id")
    run_status = kernel.add_parser("status"); run_status.add_argument("run_id")
    run_logs = kernel.add_parser("logs"); run_logs.add_argument("run_id"); run_logs.add_argument("--follow", action="store_true")
    outputs = kernel.add_parser("outputs"); outputs.add_argument("run_id")
    out_download = kernel.add_parser("download", help="List outputs if PATH is omitted, or download one selected artifact.")
    out_download.add_argument("run_id"); out_download.add_argument("path", nargs="?"); out_download.add_argument("--output")
    version_download = kernel.add_parser("download-version"); version_download.add_argument("kernel_id"); version_download.add_argument("version", type=int); version_download.add_argument("--output", required=True)
    return parser


def _render(value: Any, output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(value, indent=2, ensure_ascii=False, default=str))
    elif isinstance(value, str):
        print(value)
    else:
        print(pprint.pformat(value, sort_dicts=False, width=100))


def execute(args: argparse.Namespace, client: StudioClient) -> Any:
    q = quote
    if args.command == "status": return redact_notebook_links(client.json("GET", "/api/dashboard"))
    if args.command == "gpus": return client.json("GET", "/api/dashboard")["gpus"]
    if args.command == "estimate":
        dashboard = client.json("GET", "/api/dashboard")
        gpu = next((item for item in dashboard["gpus"] if item["key"] == args.gpu), None)
        if gpu is None:
            available = ", ".join(item["key"] for item in dashboard["gpus"])
            raise StudioError(f"Unknown GPU {args.gpu!r}. Choose one of: {available}.")
        if args.cpus not in dashboard["cpu_choices"]:
            raise StudioError(f"Unsupported CPU count {args.cpus}. Choose one of: {dashboard['cpu_choices']}.")
        if args.ram not in dashboard["ram_choices_gib"]:
            raise StudioError(f"Unsupported RAM size {args.ram} GiB. Choose one of: {dashboard['ram_choices_gib']}.")
        if args.hours <= 0:
            raise StudioError("Requested hours must be greater than zero.")
        hourly_rate = (gpu["gpu_usd_per_hour"]
                       + args.cpus * dashboard["cpu_usd_per_core_hour"]
                       + args.ram * dashboard["ram_usd_per_gib_hour"])
        budget = dashboard["budget"]
        available_usd = max(0.0, float(budget["available_usd"]))
        max_hours = min(float(args.hours), float(dashboard["max_session_hours"]), available_usd / hourly_rate)
        return {
            "gpu": gpu["key"], "gpu_name": gpu["name"], "vram_gib": gpu["vram_gib"],
            "cpus": args.cpus, "memory_gib": args.ram,
            "requested_hours": args.hours, "estimated_max_hours": round(max_hours, 6),
            "estimated_hourly_rate_usd": round(hourly_rate, 6),
            "estimated_max_cost_usd": round(hourly_rate * max_hours, 6),
            "available_budget_usd": round(available_usd, 6),
            "can_reserve_one_minute": max_hours * 3600 >= 60,
            "note": "Estimate covers sessions launched here; the Modal dashboard is authoritative.",
        }
    if args.command == "usage": return redact_notebook_links(client.json("GET", "/api/usage"))
    if args.command == "sessions":
        if args.action == "list":
            return redact_notebook_links(client.json("GET", "/api/dashboard")["sessions"])
        if args.action == "start":
            return redact_notebook_links(client.json("POST", "/api/sessions", {
                "gpu": args.gpu, "cpus": args.cpus, "memory_gib": args.ram,
                "max_hours": args.hours, "idle_timeout_minutes": args.idle_minutes,
            }))
        if args.action == "stop": return client.json("POST", f"/api/sessions/{q(args.session_id, safe='')}/stop", {})
        if args.action == "status": return client.json("GET", f"/api/sessions/{q(args.session_id, safe='')}/status")
        if args.action == "open":
            return open_session(client, args.session_id)
        if args.action == "logs":
            path=f"/api/sessions/{q(args.session_id, safe='')}/events" if args.follow else f"/api/sessions/{q(args.session_id, safe='')}/logs"
            if args.follow: return client.events(path, stop_when_complete=True)
            return client.json("GET", path)
    if args.command == "datasets":
        if args.action == "list": return client.json("GET", "/api/dashboard")["datasets"]
        if args.action == "upload":
            part=_file_part(args.file)
            try: return client.upload("/api/datasets", files=[("file", part)])
            finally: part[1].close()
        if args.action == "delete": return client.json("DELETE", f"/api/datasets/{q(args.dataset_id, safe='')}")
        if args.action == "download": return client.download(f"/api/datasets/{q(args.dataset_id, safe='')}/download", args.output)
    if args.command == "storage":
        if args.action == "list": return client.json("GET", "/api/storage", params={"path": args.path})
        if args.action == "upload":
            part=_file_part(args.file)
            try: return client.upload("/api/storage/files", fields={"path": args.path}, files=[("file", part)])
            finally: part[1].close()
        if args.action == "download": return client.download("/api/storage/download", args.output, params={"path": args.path})
        if args.action == "delete": return client.json("DELETE", "/api/storage/files", params={"path": args.path})
    if args.command == "account":
        if args.action == "status": return client.json("GET", "/api/modal-credentials")
        if args.action == "forget": return client.json("DELETE", "/api/modal-credentials")
        if args.action == "connect":
            token_id=args.token_id or input("Modal token ID: ").strip()
            token_secret=args.token_secret or getpass.getpass("Modal token secret: ")
            return client.json("POST", "/api/modal-credentials", {"token_id":token_id,"token_secret":token_secret})
    if args.command == "users" and args.action == "provision":
        password=args.password or getpass.getpass("New user password (12+ characters): ")
        key=os.getenv("NOTEBOOK_STUDIO_ADMIN_KEY", "")
        if not key: raise StudioError("Set NOTEBOOK_STUDIO_ADMIN_KEY for account provisioning.")
        return client.json("POST", "/api/admin/users", {"username":args.username,"password":password},
                           headers={"X-Admin-Provisioning-Key":key})
    if args.command == "kernel":
        if args.action == "list": return client.json("GET", "/api/kernels")
        if args.action == "create": return client.json("POST", "/api/kernels", {"name":args.name})
        if args.action == "push":
            kernel_id=args.kernel_id
            created=None
            if not kernel_id:
                name=args.name or Path(args.notebook).stem
                created=client.json("POST", "/api/kernels", {"name":name})["kernel"]
                kernel_id=created["id"]
            notebook_part=_file_part(args.notebook)
            files=[("notebook", (Path(args.notebook).name, notebook_part[1], notebook_part[2]))]
            handles=[notebook_part[1]]
            for attachment in args.attachment:
                part=_file_part(attachment, upload_name=attachment_archive_name(args.notebook, attachment))
                files.append(("attachments", part)); handles.append(part[1])
            try:
                result=client.upload(f"/api/kernels/{q(kernel_id, safe='')}/versions", files=files)
                return {"created_kernel":created,"push":result}
            finally:
                for handle in handles: handle.close()
        if args.action == "versions": return client.json("GET", f"/api/kernels/{q(args.kernel_id, safe='')}/versions")
        if args.action == "benchmark":
            result=client.json("POST", f"/api/kernels/{q(args.kernel_id, safe='')}/runs",
                               {"version":args.version,"timeout_seconds":args.timeout,"kind":"benchmark"})["run"]
            if args.follow: return client.events(f"/api/kernels/runs/{q(result['id'], safe='')}/events")
            return result
        if args.action == "runs": return client.json("GET", f"/api/kernels/{q(args.kernel_id, safe='')}/runs")
        if args.action == "status": return client.json("GET", f"/api/kernels/runs/{q(args.run_id, safe='')}")
        if args.action == "logs":
            if args.follow: return client.events(f"/api/kernels/runs/{q(args.run_id, safe='')}/events")
            return client.json("GET", f"/api/kernels/runs/{q(args.run_id, safe='')}")["logs"]
        if args.action == "outputs": return client.json("GET", f"/api/kernels/runs/{q(args.run_id, safe='')}/outputs")
        if args.action == "download":
            if not args.path: return client.json("GET", f"/api/kernels/runs/{q(args.run_id, safe='')}/outputs")
            if not args.output: raise StudioError("Specify --output when downloading an artifact.")
            return client.download(f"/api/kernels/runs/{q(args.run_id, safe='')}/outputs/download", args.output,
                                   params={"path":args.path})
        if args.action == "download-version":
            return client.download(f"/api/kernels/{q(args.kernel_id, safe='')}/versions/{args.version}/download", args.output)
    raise StudioError("Unsupported command.")


def main(argv: list[str] | None = None) -> int:
    parser=make_parser()
    args=parser.parse_args(argv)
    try:
        load_cli_env()
        password=args.password or os.getenv("NOTEBOOK_STUDIO_PASSWORD")
        if not password and sys.stdin.isatty(): password=getpass.getpass("Notebook Studio password: ")
        if not password: raise StudioError("Set NOTEBOOK_STUDIO_PASSWORD or pass --password.")
        client=StudioClient(args.url,args.username,password)
        try:
            result=execute(args,client)
            if result is not None: _render(result,args.format)
        finally:
            client.close()
        return 0
    except StudioError as exc:
        print(json.dumps({"error":str(exc)}),file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
