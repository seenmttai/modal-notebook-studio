import { ModalClient, Probe } from "npm:modal@0.10.1";

const GPU_OPTIONS = [
  { key: "T4", name: "NVIDIA T4", vram_gib: 16, gpu_usd_per_hour: 0.5904 },
  { key: "L4", name: "NVIDIA L4", vram_gib: 24, gpu_usd_per_hour: 0.7992 },
  { key: "A10", name: "NVIDIA A10", vram_gib: 24, gpu_usd_per_hour: 1.1016 },
  { key: "L40S", name: "NVIDIA L40S", vram_gib: 48, gpu_usd_per_hour: 1.9512 },
  {
    key: "A100-40GB",
    name: "NVIDIA A100 40 GB",
    vram_gib: 40,
    gpu_usd_per_hour: 2.0988,
  },
  {
    key: "A100-80GB",
    name: "NVIDIA A100 80 GB",
    vram_gib: 80,
    gpu_usd_per_hour: 2.4984,
  },
  {
    key: "RTX-PRO-6000",
    name: "NVIDIA RTX PRO 6000",
    vram_gib: 96,
    gpu_usd_per_hour: 3.0312,
  },
  {
    key: "H100",
    name: "NVIDIA H100",
    vram_gib: 80,
    gpu_usd_per_hour: 3.9492,
    note: "Modal may route H100 to H200.",
  },
  { key: "H200", name: "NVIDIA H200", vram_gib: 141, gpu_usd_per_hour: 4.5396 },
  { key: "B200", name: "NVIDIA B200", vram_gib: 180, gpu_usd_per_hour: 6.2496 },
  {
    key: "B300",
    name: "NVIDIA B300",
    vram_gib: 288,
    gpu_usd_per_hour: 7.0992,
    note: "Confirm CUDA 13.1+ support in your workload.",
  },
];
const CPU_CHOICES = [2, 4, 8, 16];
const RAM_CHOICES_GIB = [8, 16, 32, 64, 128];
const CPU_PER_CORE_HOUR = 0.141912;
const RAM_PER_GIB_HOUR = 0.024012;
const MAX_UPLOAD_BYTES = 32 * 1024 * 1024;
const MAX_DOWNLOAD_BYTES = 16 * 1024 * 1024;
const APP_BUDGET_USD = 29;
const MAX_HOURS = 8;
const APP_PREFIX = "notebook-studio";

interface Credentials {
  tokenId: string;
  tokenSecret: string;
}

interface SessionRequest {
  gpu: string;
  cpus: number;
  memory_gib: number;
  max_hours: number;
  idle_timeout_minutes: number;
}

const mimeTypes: Record<string, string> = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
};

function json(data: unknown, status = 200, headers?: HeadersInit): Response {
  const responseHeaders = new Headers(headers);
  responseHeaders.set("content-type", "application/json; charset=utf-8");
  responseHeaders.set("cache-control", "no-store");
  return new Response(JSON.stringify(data), {
    status,
    headers: responseHeaders,
  });
}

function error(message: string, status: number): Response {
  return json({ detail: message }, status);
}

function safeError(errorValue: unknown): { detail: string; status: number } {
  const rawCode =
    errorValue && typeof errorValue === "object" && "code" in errorValue
      ? String((errorValue as { code: unknown }).code)
      : "";
  if (rawCode === "16" || /unauthenticated/i.test(String(errorValue))) {
    return {
      detail:
        "Modal rejected this token. Check the token ID, secret, and workspace.",
      status: 401,
    };
  }
  if (/launch a notebook session/i.test(String(errorValue))) {
    return {
      detail: "Launch a notebook session before using its persistent Volume.",
      status: 409,
    };
  }
  if (/must be downloaded from JupyterLab/i.test(String(errorValue))) {
    return {
      detail: "Files over 16 MiB must be downloaded from JupyterLab.",
      status: 413,
    };
  }
  if (rawCode === "5" || /not.?found/i.test(String(errorValue))) {
    return {
      detail:
        "The requested Modal resource was not found or is no longer running.",
      status: 404,
    };
  }
  return {
    detail:
      "Modal request failed. Check the token, network, GPU availability, and Modal dashboard.",
    status: 502,
  };
}

function getCredentials(request: Request): Credentials | null {
  const tokenId = request.headers.get("x-modal-token-id")?.trim() || "";
  const tokenSecret = request.headers.get("x-modal-token-secret") || "";
  if (
    !tokenId || !tokenSecret || tokenId.length > 256 ||
    tokenSecret.length > 2048
  ) return null;
  return { tokenId, tokenSecret };
}

const CORS_ALLOWED_ORIGINS = new Set([
  "https://modal-notebook-studio-staging.bhansalimanan55.workers.dev",
  "https://modal-notebook-studio.bhansalimanan55.workers.dev",
  "http://127.0.0.1:8765",
  "http://localhost:8765",
]);

function isAllowedOrigin(request: Request): boolean {
  const origin = request.headers.get("origin");
  return !origin || origin === new URL(request.url).origin ||
    CORS_ALLOWED_ORIGINS.has(origin);
}

function withCors(request: Request, response: Response): Response {
  const origin = request.headers.get("origin");
  if (!origin) return response;
  const headers = new Headers(response.headers);
  headers.set("access-control-allow-origin", origin);
  headers.set(
    "access-control-allow-headers",
    "content-type,x-modal-token-id,x-modal-token-secret,x-studio-session",
  );
  headers.set("access-control-allow-methods", "GET, POST, PUT, DELETE, OPTIONS");
  headers.set("vary", "Origin");
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}

function assertSameOrigin(request: Request): Response | null {
  if (!isAllowedOrigin(request)) {
    return error("Cross-origin requests are not accepted.", 403);
  }
  return null;
}

async function withModal<T>(
  credentials: Credentials,
  operation: (client: ModalClient) => Promise<T>,
): Promise<T> {
  const client = new ModalClient({
    tokenId: credentials.tokenId,
    tokenSecret: credentials.tokenSecret,
    timeoutMs: 20_000,
    maxRetries: 1,
    logLevel: "error",
  });
  try {
    return await operation(client);
  } finally {
    client.close();
  }
}

function validWorkspacePath(raw: string, allowRoot = false): string {
  const path = raw.replaceAll("\\", "/").trim();
  if (allowRoot && (path === "" || path === "/")) return "/workspace";
  if (!path.startsWith("/") || path.includes("\0")) {
    throw new Error("Path must start with /.");
  }
  const parts = path.split("/").filter(Boolean);
  if (!parts.length || parts.some((part) => part === "." || part === "..")) {
    throw new Error("Relative path components are not allowed.");
  }
  const allowed = new Set([
    "notebooks",
    "datasets",
    "models",
    "checkpoints",
    "outputs",
    "caches",
    "kernels",
  ]);
  if (!allowed.has(parts[0])) {
    throw new Error(
      "Path must be inside notebooks, datasets, models, checkpoints, outputs, caches, or kernels.",
    );
  }
  return `/workspace/${parts.join("/")}`;
}

function safeFilename(raw: string): string {
  const leaf = raw.replaceAll("\\", "/").split("/").pop() || "dataset";
  return leaf.replace(/[^A-Za-z0-9._ -]/g, "_").replace(/^[ .]+|[ .]+$/g, "")
    .slice(0, 150) || "dataset";
}

async function namespaceFor(tokenId: string): Promise<string> {
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(tokenId),
  );
  return Array.from(
    new Uint8Array(digest).slice(0, 8),
    (value) => value.toString(16).padStart(2, "0"),
  ).join("");
}

function monthlyKey(): string {
  const now = new Date();
  return `${now.getUTCFullYear()}-${
    String(now.getUTCMonth() + 1).padStart(2, "0")
  }`;
}

async function readJson(request: Request): Promise<Record<string, unknown>> {
  try {
    const value = await request.json();
    return value && typeof value === "object"
      ? value as Record<string, unknown>
      : {};
  } catch {
    throw new Error("Request body must be valid JSON.");
  }
}

function findSessionId(request: Request): string {
  return request.headers.get("x-studio-session")?.trim() || "";
}

async function requireSandbox(client: ModalClient, request: Request) {
  const sessionId = findSessionId(request);
  if (!sessionId || sessionId.length > 128) {
    throw new Error(
      "Launch a notebook session before using Modal Volume files.",
    );
  }
  const sandbox = await client.sandboxes.fromId(sessionId);
  return sandbox;
}

async function handleApi(request: Request): Promise<Response> {
  const url = new URL(request.url);
  const path = url.pathname;
  const method = request.method.toUpperCase();
  if (method === "OPTIONS") {
    return new Response(null, {
      status: 204,
      headers: { "allow": "GET, POST, PUT, DELETE, OPTIONS" },
    });
  }
  if (assertSameOrigin(request)) return assertSameOrigin(request)!;

  if (path === "/api/dashboard" && method === "GET") {
    return json({
      gpus: GPU_OPTIONS.map((gpu) => ({
        ...gpu,
        hourly_rate_4cpu_32gib: gpu.gpu_usd_per_hour + 4 * CPU_PER_CORE_HOUR +
          32 * RAM_PER_GIB_HOUR,
      })),
      cpu_choices: CPU_CHOICES,
      ram_choices_gib: RAM_CHOICES_GIB,
      cpu_usd_per_core_hour: CPU_PER_CORE_HOUR,
      ram_usd_per_gib_hour: RAM_PER_GIB_HOUR,
      max_session_hours: MAX_HOURS,
      upload_limit_bytes: MAX_UPLOAD_BYTES,
      budget: {
        month: monthlyKey(),
        app_limit_usd: APP_BUDGET_USD,
        spent_usd: 0,
        reserved_usd: 0,
        available_usd: APP_BUDGET_USD,
        modal_enabled: Boolean(getCredentials(request)),
        active_session: null,
      },
      sessions: [],
      datasets: [],
      modal_credentials_saved: Boolean(getCredentials(request)),
      username: "This browser",
    });
  }

  if (path === "/api/modal-credentials" && method === "POST") {
    let body: Record<string, unknown>;
    try {
      body = await readJson(request);
    } catch (cause) {
      return error((cause as Error).message, 400);
    }
    const tokenId = typeof body.token_id === "string"
      ? body.token_id.trim()
      : "";
    const tokenSecret = typeof body.token_secret === "string"
      ? body.token_secret
      : "";
    if (
      tokenId.length < 3 || tokenId.length > 256 || tokenSecret.length < 8 ||
      tokenSecret.length > 2048
    ) {
      return error("Enter a valid Modal token ID and secret.", 422);
    }
    try {
      await withModal(
        { tokenId, tokenSecret },
        (client) => client.getImageBuilderVersion(),
      );
      return json({
        connected: true,
        detail: "Token verified. It is saved only in this browser.",
      });
    } catch (cause) {
      const mapped = safeError(cause);
      return error(mapped.detail, mapped.status);
    }
  }
  if (path === "/api/modal-credentials" && method === "DELETE") {
    return json({
      connected: false,
      detail:
        "The token was removed from this browser. Revoke it in Modal separately if needed.",
    });
  }

  const credentials = getCredentials(request);
  if (!credentials) {
    return error("Connect your Modal token in Account settings first.", 401);
  }

  if (path === "/api/sessions" && method === "POST") {
    let body: Record<string, unknown>;
    try {
      body = await readJson(request);
    } catch (cause) {
      return error((cause as Error).message, 400);
    }
    const gpu = String(body.gpu || "");
    const gpuOption = GPU_OPTIONS.find((item) => item.key === gpu);
    const cpus = Number(body.cpus);
    const memoryGib = Number(body.memory_gib);
    const hours = Number(body.max_hours);
    const idleMinutes = Number(body.idle_timeout_minutes);
    if (
      !gpuOption || !CPU_CHOICES.includes(cpus) ||
      !RAM_CHOICES_GIB.includes(memoryGib) || !Number.isFinite(hours) ||
      hours <= 0 || hours > MAX_HOURS || !Number.isInteger(idleMinutes) ||
      idleMinutes < 1 || idleMinutes > 1440
    ) {
      return error(
        "GPU, CPU, RAM, runtime, or idle timeout is outside the allowed choices.",
        422,
      );
    }
    try {
      const namespace = await namespaceFor(credentials.tokenId);
      const sessionName = `studio-${
        crypto.randomUUID().replaceAll("-", "").slice(0, 12)
      }`;
      const tokenBytes = crypto.getRandomValues(new Uint8Array(32));
      const notebookToken = btoa(String.fromCharCode(...tokenBytes)).replaceAll(
        "+",
        "-",
      ).replaceAll("/", "_").replaceAll("=", "");
      const result = await withModal(credentials, async (client) => {
        const app = await client.apps.fromName(`${APP_PREFIX}-${namespace}`, {
          createIfMissing: true,
        });
        const volume = await client.volumes.fromName(
          `${APP_PREFIX}-ws-${namespace}`,
          { createIfMissing: true },
        );
        const image = client.images.fromRegistry(
          "pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime",
        );
        const command = [
          "bash",
          "-lc",
          "set -e; mkdir -p /workspace/{notebooks,datasets,models,checkpoints,outputs,caches,kernels}; " +
          "python -m pip install --disable-pip-version-check --no-warn-script-location jupyterlab ipykernel numpy pandas matplotlib scikit-learn transformers datasets accelerate safetensors > /workspace/outputs/setup.log 2>&1; " +
          "exec jupyter lab --no-browser --allow-root --ip=0.0.0.0 --port=8888 --ServerApp.root_dir=/workspace --ServerApp.allow_remote_access=True --ServerApp.allow_origin='*' --ServerApp.token=\"$JUPYTER_TOKEN\" > /workspace/outputs/jupyter.log 2>&1",
        ];
        const sandbox = await client.sandboxes.create(app, image, {
          name: sessionName,
          tags: { application: APP_PREFIX, browser_owner: namespace },
          command,
          env: {
            JUPYTER_TOKEN: notebookToken,
            HF_HOME: "/workspace/caches/huggingface",
            HF_HUB_CACHE: "/workspace/caches/huggingface/hub",
            TORCH_HOME: "/workspace/caches/torch",
            XDG_CACHE_HOME: "/workspace/caches/xdg",
          },
          volumes: { "/workspace": volume },
          gpu,
          cpu: cpus,
          memoryMiB: memoryGib * 1024,
          timeoutMs: Math.ceil(hours * 3600 * 1000),
          idleTimeoutMs: Math.min(
            Math.ceil(hours * 3600 * 1000),
            idleMinutes * 60 * 1000,
          ),
          encryptedPorts: [8888],
          readinessProbe: Probe.withTcp(8888, { intervalMs: 2_000 }),
        });
        let onAbort: (() => void) | undefined;
        try {
          // A browser can close the request while Modal is still waiting for the
          // encrypted tunnel. Race that disconnect against tunnel readiness so a
          // successful Sandbox create can never become an orphaned session.
          const clientDisconnected = new Promise<never>((_, reject) => {
            onAbort = () => reject(new Error("The client disconnected before the notebook was ready."));
            if (request.signal.aborted) onAbort();
            else request.signal.addEventListener("abort", onAbort, { once: true });
          });
          const tunnels = await Promise.race([
            sandbox.tunnels(60_000),
            clientDisconnected,
          ]);
          const tunnel = tunnels[8888];
          if (!tunnel?.url) {
            throw new Error("Modal did not return the Jupyter tunnel.");
          }
          const now = new Date().toISOString();
          return {
            id: sandbox.sandboxId,
            sandbox_id: sandbox.sandboxId,
            status: "starting",
            notebook_url: `${tunnel.url.replace(/\/$/, "")}/?token=${
              encodeURIComponent(notebookToken)
            }`,
            gpu_key: gpu,
            cpus,
            memory_gib: memoryGib,
            hourly_rate: gpuOption.gpu_usd_per_hour + cpus * CPU_PER_CORE_HOUR +
              memoryGib * RAM_PER_GIB_HOUR,
            max_runtime_seconds: Math.ceil(hours * 3600),
            idle_timeout_minutes: idleMinutes,
            started_at: now,
            reserved_usd:
              (gpuOption.gpu_usd_per_hour + cpus * CPU_PER_CORE_HOUR +
                memoryGib * RAM_PER_GIB_HOUR) * hours,
            billed_usd: 0,
            notebook_token: notebookToken,
          };
        } catch (cause) {
          await sandbox.terminate().catch(() => undefined);
          throw cause;
        } finally {
          if (onAbort) request.signal.removeEventListener("abort", onAbort);
          sandbox.detach();
        }
      });
      return json({ session: result });
    } catch (cause) {
      const mapped = safeError(cause);
      return error(mapped.detail, mapped.status);
    }
  }

  const sessionMatch = path.match(
    /^\/api\/sessions\/([^/]+)(?:\/(status|logs|events|stop))?$/,
  );
  if (sessionMatch) {
    const sessionId = decodeURIComponent(sessionMatch[1]);
    const action = sessionMatch[2] || "status";
    try {
      return await withModal(credentials, async (client) => {
        const sandbox = await client.sandboxes.fromId(sessionId);
        try {
          if (action === "stop" && method === "POST") {
            await sandbox.terminate();
            return json({ stopped: true, ended_at: new Date().toISOString() });
          }
          if (action === "logs" && method === "GET") {
            const process = await sandbox.exec([
              "bash",
              "-lc",
              'for f in /workspace/outputs/setup.log /workspace/outputs/jupyter.log; do [ ! -f "$f" ] || { echo ===="$f"; tail -n 80 "$f"; }; done',
            ], { timeoutMs: 8_000 });
            const logs = await process.stdout.readText();
            await process.wait();
            return json({ session_id: sessionId, logs });
          }
          if (action === "events" && method === "GET") {
            const exitCode = await sandbox.poll();
            const process = await sandbox.exec([
              "bash",
              "-lc",
              "if [ -f /workspace/outputs/setup.log ]; then grep -E '^(ERROR|WARNING|Successfully installed)' /workspace/outputs/setup.log | tail -n 30; fi; [ ! -f /workspace/outputs/jupyter.log ] || tail -n 30 /workspace/outputs/jupyter.log",
            ], { timeoutMs: 8_000 });
            const logs = await process.stdout.readText();
            await process.wait();
            return json({
              session_id: sessionId,
              status: exitCode === null ? "running" : "complete",
              exit_code: exitCode,
              events: logs.split("\n").filter(Boolean).map((message) => ({
                type: "log",
                message,
              })),
            });
          }
          if (action === "status" && method === "GET") {
            const exitCode = await sandbox.poll();
            let notebookReady = false;
            if (exitCode === null) {
              const probe = await sandbox.exec([
                "bash",
                "-lc",
                "python - <<'PY'\nimport urllib.request\ntry:\n urllib.request.urlopen('http://127.0.0.1:8888/api/status', timeout=2); print('ready')\nexcept Exception: print('starting')\nPY",
              ], { timeoutMs: 5_000 });
              notebookReady = (await probe.stdout.readText()).includes("ready");
              await probe.wait();
            }
            const metricsProcess = exitCode === null
              ? await sandbox.exec([
                "bash",
                "-lc",
                "python - <<'PY'\nimport json\nr={'cpu_load_1m':None,'memory_total_bytes':None,'memory_available_bytes':None,'gpu':[]}\ntry:r['cpu_load_1m']=float(open('/proc/loadavg').read().split()[0])\nexcept Exception:pass\ntry:\n d={}\n for line in open('/proc/meminfo'):\n  k,_,v=line.partition(':')\n  if k in ('MemTotal','MemAvailable'):d[k]=int(v.split()[0])*1024\n r['memory_total_bytes']=d.get('MemTotal');r['memory_available_bytes']=d.get('MemAvailable')\nexcept Exception:pass\nprint(json.dumps(r))\nPY",
              ], { timeoutMs: 5_000 })
              : null;
            let resources: unknown = null;
            if (metricsProcess) {
              const raw =
                (await metricsProcess.stdout.readText()).trim().split("\n")
                  .pop() || "{}";
              await metricsProcess.wait();
              try {
                resources = JSON.parse(raw);
              } catch {
                resources = null;
              }
            }
            return json({
              session_id: sessionId,
              status: exitCode === null
                ? (notebookReady ? "running" : "starting")
                : "complete",
              running: exitCode === null,
              notebook_ready: notebookReady,
              exit_code: exitCode,
              failure_reason: exitCode !== null && exitCode !== 0
                ? `Sandbox exited with code ${exitCode}.`
                : null,
              queue_position: null,
              worker_assignment: { sandbox_id: sessionId },
              resources,
            });
          }
          return error("Unsupported session operation.", 405);
        } finally {
          sandbox.detach();
        }
      });
    } catch (cause) {
      const mapped = safeError(cause);
      return error(mapped.detail, mapped.status);
    }
  }

  if (
    (path === "/api/datasets" || path === "/api/storage") && method === "GET"
  ) {
    try {
      return await withModal(credentials, async (client) => {
        const sandbox = await requireSandbox(client, request);
        try {
          const requested = path === "/api/datasets"
            ? "/workspace/datasets"
            : validWorkspacePath(url.searchParams.get("path") || "/", true);
          const entries = await sandbox.filesystem.listFiles(requested);
          if (path === "/api/storage") {
            return json({
              path: requested.replace("/workspace", ""),
              files: entries,
            });
          }
          const datasets = [];
          for (const directory of entries) {
            if (directory.type === "directory") {
              for (
                const item of await sandbox.filesystem.listFiles(directory.path)
              ) {
                if (item.type === "file") {
                  datasets.push({
                    id: directory.name,
                    name: item.name,
                    path: item.path.replace("/workspace", ""),
                    size_bytes: item.size,
                    uploaded_at: new Date(item.modifiedTime * 1000)
                      .toISOString(),
                  });
                }
              }
            } else if (directory.type === "file") {
              datasets.push({
                id: directory.name,
                name: directory.name,
                path: directory.path.replace("/workspace", ""),
                size_bytes: directory.size,
                uploaded_at: new Date(directory.modifiedTime * 1000)
                  .toISOString(),
              });
            }
          }
          return json({ datasets });
        } finally {
          sandbox.detach();
        }
      });
    } catch (cause) {
      const mapped = safeError(cause);
      return error(mapped.detail, mapped.status);
    }
  }

  if (path === "/api/datasets" && method === "POST") {
    try {
      const form = await request.formData();
      const file = form.get("file");
      if (!(file instanceof File)) {
        return error("Choose a file to upload.", 400);
      }
      if (file.size < 1 || file.size > MAX_UPLOAD_BYTES) {
        return error(
          `Upload must be between 1 byte and ${
            MAX_UPLOAD_BYTES / 1024 / 1024
          } MiB.`,
          413,
        );
      }
      const sessionId = findSessionId(request);
      if (!sessionId) {
        return error(
          "Launch a notebook session before uploading files to its persistent Volume.",
          409,
        );
      }
      const id = crypto.randomUUID();
      const name = safeFilename(file.name);
      const volumePath = `/workspace/datasets/${id}/${name}`;
      const bytes = new Uint8Array(await file.arrayBuffer());
      const result = await withModal(credentials, async (client) => {
        const sandbox = await client.sandboxes.fromId(sessionId);
        try {
          await sandbox.filesystem.writeBytes(bytes, volumePath);
          return {
            id,
            name,
            path: volumePath.replace("/workspace", ""),
            size_bytes: bytes.byteLength,
            uploaded_at: new Date().toISOString(),
          };
        } finally {
          sandbox.detach();
        }
      });
      return json({
        dataset: result,
        note: `Uploaded ${name} to your Modal Volume.`,
      });
    } catch (cause) {
      const mapped = safeError(cause);
      return error(mapped.detail, mapped.status);
    }
  }

  const datasetMatch = path.match(/^\/api\/datasets\/([^/]+)$/);
  if (datasetMatch && method === "DELETE") {
    let body: Record<string, unknown>;
    try {
      body = await readJson(request);
    } catch (cause) {
      return error((cause as Error).message, 400);
    }
    let volumePath: string;
    try {
      volumePath = validWorkspacePath(String(body.path || ""));
      if (!volumePath.startsWith("/workspace/datasets/")) {
        throw new Error("Only dataset files can be deleted here.");
      }
    } catch (cause) {
      return error((cause as Error).message, 422);
    }
    try {
      await withModal(credentials, async (client) => {
        const sandbox = await requireSandbox(client, request);
        try {
          await sandbox.filesystem.remove(volumePath);
        } finally {
          sandbox.detach();
        }
      });
      return json({
        deleted: decodeURIComponent(datasetMatch[1]),
        note: "Dataset removed from your Modal Volume.",
      });
    } catch (cause) {
      const mapped = safeError(cause);
      return error(mapped.detail, mapped.status);
    }
  }

  if (path === "/api/storage/files" && method === "PUT") {
    let volumePath: string;
    try {
      volumePath = validWorkspacePath(url.searchParams.get("path") || "");
    } catch (cause) {
      return error((cause as Error).message, 422);
    }
    if (!request.body) return error("Upload body is empty.", 400);
    const buffer = new Uint8Array(await request.arrayBuffer());
    if (!buffer.length || buffer.length > MAX_UPLOAD_BYTES) {
      return error("Storage uploads are limited to 32 MiB per file.", 413);
    }
    try {
      await withModal(credentials, async (client) => {
        const sandbox = await requireSandbox(client, request);
        try {
          await sandbox.filesystem.writeBytes(buffer, volumePath);
        } finally {
          sandbox.detach();
        }
      });
      return json({
        file: {
          path: volumePath.replace("/workspace", ""),
          size_bytes: buffer.byteLength,
        },
      });
    } catch (cause) {
      const mapped = safeError(cause);
      return error(mapped.detail, mapped.status);
    }
  }

  if (path === "/api/storage/download" && method === "GET") {
    let volumePath: string;
    try {
      volumePath = validWorkspacePath(url.searchParams.get("path") || "");
    } catch (cause) {
      return error((cause as Error).message, 422);
    }
    try {
      const bytes = await withModal(credentials, async (client) => {
        const sandbox = await requireSandbox(client, request);
        try {
          const info = await sandbox.filesystem.stat(volumePath);
          if (info.type !== "file") {
            throw new Error("Only files can be downloaded.");
          }
          if (info.size > MAX_DOWNLOAD_BYTES) {
            throw new Error(
              "Files larger than 16 MiB must be downloaded from JupyterLab.",
            );
          }
          return await sandbox.filesystem.readBytes(volumePath);
        } finally {
          sandbox.detach();
        }
      });
      const filename = volumePath.split("/").pop() || "download";
      return new Response(bytes.buffer as ArrayBuffer, {
        headers: {
          "content-type": "application/octet-stream",
          "content-length": String(bytes.byteLength),
          "content-disposition": `attachment; filename*=UTF-8''${
            encodeURIComponent(filename)
          }`,
          "cache-control": "no-store",
        },
      });
    } catch (cause) {
      const mapped = safeError(cause);
      return error(mapped.detail, mapped.status);
    }
  }

  if (path === "/api/storage/files" && method === "DELETE") {
    let volumePath: string;
    try {
      volumePath = validWorkspacePath(url.searchParams.get("path") || "");
    } catch (cause) {
      return error((cause as Error).message, 422);
    }
    try {
      await withModal(credentials, async (client) => {
        const sandbox = await requireSandbox(client, request);
        try {
          await sandbox.filesystem.remove(volumePath);
        } finally {
          sandbox.detach();
        }
      });
      return json({ deleted: volumePath.replace("/workspace", "") });
    } catch (cause) {
      const mapped = safeError(cause);
      return error(mapped.detail, mapped.status);
    }
  }

  return error("API route not found.", 404);
}

export async function handleRequest(request: Request): Promise<Response> {
  const url = new URL(request.url);
  if (url.pathname === "/health") return json({ status: "ok" });
  if (url.pathname.startsWith("/api/")) {
    if (!isAllowedOrigin(request)) {
      return error("Cross-origin requests are not accepted.", 403);
    }
    if (request.method.toUpperCase() === "OPTIONS") {
      return withCors(
        request,
        new Response(null, {
          status: 204,
          headers: { "allow": "GET, POST, PUT, DELETE, OPTIONS" },
        }),
      );
    }
    return withCors(request, await handleApi(request));
  }
  return error("Not found.", 404);
}

if (import.meta.main) {
  Deno.serve({ port: Number(Deno.env.get("PORT") || 8000) }, handleRequest);
}
