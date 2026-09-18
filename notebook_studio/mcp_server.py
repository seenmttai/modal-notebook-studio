from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import quote

from .cli import (
    StudioClient, attachment_archive_name, load_cli_env, open_session, redact_notebook_links,
)

try:
    from mcp.server import MCPServer
except ImportError as exc:  # pragma: no cover - optional dependency
    raise RuntimeError('Install the MCP extra with: pip install "modal-notebook-studio[mcp]"') from exc


mcp = MCPServer(
    "notebook-studio",
    instructions=(
        "Notebook Studio is a local controller for this user's own Modal account. "
        "Use dashboard and usage before GPU launches. Every benchmark run is linked to one stable kernel ID "
        "and an immutable numbered version; benchmark runs are separate from notebook session history. "
        "Always inspect structured run status and outputs after execution. The service reports null when Modal "
        "does not expose queue or worker-host details instead of guessing."
    ),
)


def _with_client(operation):
    load_cli_env()
    client = StudioClient.from_env(prompt=False)
    try:
        return operation(client)
    finally:
        client.close()


def _q(value: str) -> str:
    return quote(value, safe="")


def _upload_part(client: StudioClient, local_path: str):
    """Open a local upload after checking the same size and empty-file rules as the GUI."""
    local = Path(local_path).expanduser()
    if not local.is_file():
        raise ValueError(f"Upload source must be a regular file on the MCP server machine: {local}")
    size = local.stat().st_size
    if size == 0:
        raise ValueError("Empty files are not accepted.")
    limit = client.json("GET", "/api/dashboard").get("upload_limit_bytes")
    if isinstance(limit, int) and size > limit:
        raise ValueError(
            f"File is {size} bytes, above this workspace's upload limit of {limit} bytes."
        )
    return local, (local.name, local.open("rb"), "application/octet-stream")


@mcp.resource("notebook-studio://dashboard")
def dashboard_resource() -> dict:
    """Current dashboard, including budget headroom, GPU options, sessions, and datasets."""
    return _with_client(lambda client: redact_notebook_links(client.json("GET", "/api/dashboard")))


@mcp.resource("notebook-studio://usage")
def usage_resource() -> dict:
    """Current app-side monthly estimate; Modal's invoice remains authoritative."""
    return _with_client(lambda client: redact_notebook_links(client.json("GET", "/api/usage")))


@mcp.resource("notebook-studio://kernels")
def kernels_resource() -> dict:
    """Stable kernel IDs and their latest immutable version numbers."""
    return _with_client(lambda client: client.json("GET", "/api/kernels"))


@mcp.tool()
def studio_status() -> dict:
    """Read the full dashboard before choosing compute or changing workspace state."""
    return dashboard_resource()


@mcp.tool()
def gpu_options() -> list[dict]:
    """List every allowed GPU with VRAM, notes, and estimated GPU plus CPU/RAM rates."""
    return dashboard_resource()["gpus"]


@mcp.tool()
def usage_status() -> dict:
    """Read app-estimated monthly spend, reserved budget, available headroom, and caveats."""
    return usage_resource()


@mcp.tool()
def session_list() -> list[dict]:
    """List recent notebook sessions, GPU selections, resources, runtime caps, and history."""
    return dashboard_resource()["sessions"]


@mcp.tool()
def session_start(gpu: str, cpus: int = 4, memory_gib: int = 32,
                  max_hours: float = 2.0, idle_timeout_minutes: int = 15) -> dict:
    """Start JupyterLab on one GPU after the local budget guard reserves its estimated maximum cost."""
    return _with_client(lambda client: redact_notebook_links(client.json("POST", "/api/sessions", {
        "gpu": gpu, "cpus": cpus, "memory_gib": memory_gib, "max_hours": max_hours,
        "idle_timeout_minutes": idle_timeout_minutes,
    })))


@mcp.tool()
def session_status(session_id: str) -> dict:
    """Get structured lifecycle status, queue/worker visibility, requested and observed resources, and failures."""
    return _with_client(lambda client: client.json("GET", f"/api/sessions/{_q(session_id)}/status"))


@mcp.tool()
def session_open(session_id: str) -> dict:
    """Open a running session's JupyterLab in the MCP host browser without returning its bearer URL."""
    return _with_client(lambda client: open_session(client, session_id))


@mcp.tool()
def session_stop(session_id: str) -> dict:
    """Stop a running notebook session and release its unused budget reservation."""
    return _with_client(lambda client: client.json("POST", f"/api/sessions/{_q(session_id)}/stop", {}))


@mcp.tool()
def session_logs(session_id: str, limit: int = 100) -> dict:
    """Read the latest Modal entrypoint logs for a notebook session."""
    return _with_client(lambda client: client.json("GET", f"/api/sessions/{_q(session_id)}/logs", params={"limit": limit}))


@mcp.tool()
def session_events(session_id: str, max_wait_seconds: int = 20) -> dict:
    """Follow a bounded slice of live session logs and status events (maximum 60 seconds)."""
    return _with_client(lambda client: client.collect_events(
        f"/api/sessions/{_q(session_id)}/events", max_wait_seconds=max_wait_seconds
    ))


@mcp.tool()
def dataset_list() -> list[dict]:
    """List uploaded datasets in this login's Modal Volume, with IDs, names, paths, sizes, and upload dates."""
    return dashboard_resource()["datasets"]


@mcp.tool()
def dataset_upload(local_path: str) -> dict:
    """Upload one non-empty local file into this login's persistent Modal datasets folder.

    local_path is a filesystem path visible to the machine running the MCP server, not the AI host.
    The tool checks the current dashboard upload limit before sending file bytes. This writes the file to
    the connected user's Modal Volume and adds it to the GUI dataset list; call dataset_list to verify.
    """
    def upload(client):
        _, part = _upload_part(client, local_path)
        try:
            return client.upload("/api/datasets", files=[("file", part)])
        finally:
            part[1].close()
    return _with_client(upload)


@mcp.tool()
def dataset_delete(dataset_id: str) -> dict:
    """Permanently delete the dataset with this exact dataset ID from the Modal Volume.

    This removes the file itself. Verify the ID with dataset_list and obtain the user's intent before calling.
    """
    return _with_client(lambda client: client.json("DELETE", f"/api/datasets/{_q(dataset_id)}"))


@mcp.tool()
def dataset_download(dataset_id: str, local_path: str) -> dict:
    """Download one dataset ID to local_path on the machine running the MCP server.

    Use dataset_list first to resolve the exact ID. The destination's parent directory is created if needed.
    """
    return _with_client(lambda client: client.download(f"/api/datasets/{_q(dataset_id)}/download", local_path))


@mcp.tool()
def storage_list(path: str = "/") -> dict:
    """List files in this login's persistent Modal Volume.

    path is a Volume path such as /models or /workspace/outputs; / lists the Volume root.
    Allowed folders are notebooks, datasets, models, checkpoints, outputs, caches, and kernels.
    """
    return _with_client(lambda client: client.json("GET", "/api/storage", params={"path": path}))


@mcp.tool()
def storage_upload(local_path: str, workspace_directory: str) -> dict:
    """Upload one non-empty local file into an allowed persistent workspace folder.

    local_path must exist on the machine running the MCP server. workspace_directory is a Volume
    directory such as /models, /checkpoints, /outputs, /caches, /kernels, or /notebooks.
    The server checks the upload limit and path safety. Use dataset_upload for /datasets so the dataset
    listing stays synchronized. Uploads land with the local file's basename.
    """
    def upload(client):
        _, part = _upload_part(client, local_path)
        try:
            return client.upload("/api/storage/files", fields={"path": workspace_directory}, files=[("file", part)])
        finally:
            part[1].close()
    return _with_client(upload)


@mcp.tool()
def storage_download(workspace_path: str, local_path: str) -> dict:
    """Download one Volume file to a path on the machine running the MCP server.

    workspace_path is a Volume path such as /outputs/result.csv; local_path is the destination path.
    """
    return _with_client(lambda client: client.download("/api/storage/download", local_path,
                                                       params={"path": workspace_path}))


@mcp.tool()
def storage_delete(workspace_path: str) -> dict:
    """Permanently delete one file from this login's persistent Modal Volume.

    workspace_path must identify the exact file, such as /outputs/result.csv. Check storage_list first
    and obtain the user's intent before deleting. Deleting a dataset this way also removes its dataset-list row.
    """
    return _with_client(lambda client: client.json("DELETE", "/api/storage/files", params={"path": workspace_path}))


@mcp.tool()
def modal_account_status() -> dict:
    """Check whether this Notebook Studio login has a Modal token saved; the token secret is never returned."""
    return _with_client(lambda client: client.json("GET", "/api/modal-credentials"))


@mcp.tool()
def studio_account_status() -> dict:
    """Show the signed-in Notebook Studio username and whether its Modal credentials are connected/saved.

    This mirrors the Account page's signed-in identity and connection indicator. It never returns credentials.
    """
    dashboard = dashboard_resource()
    return {
        "username": dashboard.get("username"),
        "modal_connected": dashboard.get("modal_credentials_connected", False),
        "modal_credentials_saved": dashboard.get("modal_credentials_saved", False),
        "workspace_volume_name": dashboard.get("workspace_volume_name"),
    }


@mcp.tool()
def modal_account_connect(token_id: str, token_secret: str) -> dict:
    """Verify and save the supplied Modal token encrypted for this Notebook Studio login.

    The token ID and secret are sensitive tool arguments: an MCP host may retain them in transcripts or logs.
    Prefer the interactive notebook-studio account connect command for secret entry. Never repeat the
    secret in follow-up messages. Forgetting the token here does not revoke it at Modal.
    """
    return _with_client(lambda client: client.json("POST", "/api/modal-credentials",
                                                    {"token_id": token_id, "token_secret": token_secret}))


@mcp.tool()
def modal_account_forget() -> dict:
    """Remove the saved encrypted Modal token for this login; this does not revoke it at Modal.

    Confirm the user intends to disconnect before calling. Revoke a compromised token separately in Modal.
    """
    return _with_client(lambda client: client.json("DELETE", "/api/modal-credentials"))


@mcp.tool()
def kernel_list() -> dict:
    """List stable kernel IDs, latest immutable versions, and benchmark run counts."""
    return _with_client(lambda client: client.json("GET", "/api/kernels"))


@mcp.tool()
def kernel_create(name: str) -> dict:
    """Create a stable kernel ID whose later numbered versions retain their own attachments and history."""
    return _with_client(lambda client: client.json("POST", "/api/kernels", {"name": name}))


@mcp.tool()
def kernel_versions(kernel_id: str) -> dict:
    """List the immutable version numbers, bundle hashes, attachments count, and notebook cell counts."""
    return _with_client(lambda client: client.json("GET", f"/api/kernels/{_q(kernel_id)}/versions"))


@mcp.tool()
def kernel_push(kernel_id: str, notebook_path: str, attachment_paths: list[str] | None = None) -> dict:
    """Push a new immutable version to an existing stable kernel ID, packaging the notebook and attachments together."""
    def upload(client):
        paths = [Path(notebook_path).expanduser(), *(Path(value).expanduser() for value in (attachment_paths or []))]
        handles=[]; files=[]
        try:
            notebook=paths[0]; handle=notebook.open("rb"); handles.append(handle)
            files.append(("notebook", (notebook.name, handle, "application/x-ipynb+json")))
            for attachment in paths[1:]:
                handle=attachment.open("rb"); handles.append(handle)
                files.append(("attachments", (attachment_archive_name(notebook_path, attachment), handle,
                                                "application/octet-stream")))
            return client.upload(f"/api/kernels/{_q(kernel_id)}/versions", files=files)
        finally:
            for handle in handles: handle.close()
    return _with_client(upload)


@mcp.tool()
def kernel_run_start(kernel_id: str, version: int, timeout_seconds: int = 3600) -> dict:
    """Queue a distinct benchmark run for a numbered kernel version in the active GPU session."""
    return _with_client(lambda client: client.json("POST", f"/api/kernels/{_q(kernel_id)}/runs",
        {"version": version, "timeout_seconds": timeout_seconds, "kind": "benchmark"}))


@mcp.tool()
def kernel_run_status(run_id: str) -> dict:
    """Read structured benchmark status, cell progress, logs, worker ID visibility, and failure reason."""
    return _with_client(lambda client: client.json("GET", f"/api/kernels/runs/{_q(run_id)}"))


@mcp.tool()
def kernel_run_events(run_id: str, max_wait_seconds: int = 20) -> dict:
    """Follow a bounded slice of benchmark logs, cell progress, and status events (maximum 60 seconds)."""
    return _with_client(lambda client: client.collect_events(
        f"/api/kernels/runs/{_q(run_id)}/events", max_wait_seconds=max_wait_seconds
    ))


@mcp.tool()
def kernel_run_history(kernel_id: str) -> dict:
    """List benchmark runs for a stable kernel ID, separate from notebook session history."""
    return _with_client(lambda client: client.json("GET", f"/api/kernels/{_q(kernel_id)}/runs"))


@mcp.tool()
def kernel_run_wait(run_id: str, max_wait_seconds: int = 60) -> dict:
    """Poll one benchmark run for at most 60 seconds and return its final/current status and log tail."""
    deadline=time.monotonic()+max(1,min(60,max_wait_seconds))
    while True:
        data=_with_client(lambda client: client.json("GET", f"/api/kernels/runs/{_q(run_id)}"))
        if data.get("status") not in {"queued","running"} or time.monotonic() >= deadline: return data
        time.sleep(1)


@mcp.tool()
def kernel_run_outputs(run_id: str) -> dict:
    """List artifacts for one benchmark run; use kernel_output_download to fetch only selected files."""
    return _with_client(lambda client: client.json("GET", f"/api/kernels/runs/{_q(run_id)}/outputs"))


@mcp.tool()
def kernel_output_download(run_id: str, artifact_path: str, local_path: str) -> dict:
    """Download one selected benchmark output artifact to the MCP server machine."""
    return _with_client(lambda client: client.download(
        f"/api/kernels/runs/{_q(run_id)}/outputs/download", local_path, params={"path": artifact_path}))


@mcp.tool()
def kernel_version_download(kernel_id: str, version: int, local_path: str) -> dict:
    """Download a complete immutable notebook version bundle, including its attachments."""
    return _with_client(lambda client: client.download(
        f"/api/kernels/{_q(kernel_id)}/versions/{version}/download", local_path))


@mcp.prompt()
def benchmark_review(kernel_id: str, version: str) -> str:
    """Prepare an AI agent to inspect a kernel version and its benchmark run carefully."""
    return (f"Inspect Notebook Studio kernel {kernel_id}, immutable version {version}. First read its version metadata, "
            "then confirm the intended GPU session and budget headroom. Start a benchmark only with user intent. "
            "After it finishes, check structured status, failure reason, streaming events/logs, and output artifacts. "
            "Benchmark history is separate from the Jupyter session list.")


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
