import { handleRequest } from "./main.ts";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

async function body(response: Response): Promise<Record<string, unknown>> {
  return await response.json() as Record<string, unknown>;
}

const sameOriginHeaders = { origin: "https://studio.test" };
const modalHeaders = {
  origin: "https://studio.test",
  "x-modal-token-id": "test-token-id",
  "x-modal-token-secret": "test-token-secret",
};

Deno.test("health is available without a Modal token", async () => {
  const response = await handleRequest(
    new Request("https://studio.test/health"),
  );
  assert(response.status === 200, "health should return HTTP 200");
  const value = await body(response);
  assert(value.status === "ok", "health status should be ok");
});

Deno.test("dashboard returns GPU catalog without a local helper", async () => {
  const response = await handleRequest(
    new Request("https://studio.test/api/dashboard", {
      headers: sameOriginHeaders,
    }),
  );
  assert(response.status === 200, "dashboard should return HTTP 200");
  const value = await body(response);
  const gpus = value.gpus as Array<Record<string, unknown>>;
  assert(gpus.some((gpu) => gpu.key === "T4"), "GPU catalog should include T4");
  assert(
    gpus.some((gpu) => gpu.key === "RTX-PRO-6000"),
    "GPU catalog should include RTX PRO 6000",
  );
  assert(
    value.upload_limit_bytes === 32 * 1024 * 1024,
    "upload limit should be visible to the UI",
  );
});

Deno.test("the frontend is served from the dynamic application", async () => {
  const response = await handleRequest(new Request("https://studio.test/"));
  assert(response.status === 200, "index should return HTTP 200");
  assert(
    (response.headers.get("content-type") || "").includes("text/html"),
    "index should have HTML content type",
  );
  const html = await response.text();
  assert(
    html.includes("Connect Modal"),
    "frontend should offer Modal connection",
  );
  assert(
    !html.includes("Connect your local helper"),
    "frontend should not ask for a local helper",
  );
});

Deno.test("invalid credentials are rejected before contacting Modal", async () => {
  const response = await handleRequest(
    new Request("https://studio.test/api/modal-credentials", {
      method: "POST",
      headers: { ...sameOriginHeaders, "content-type": "application/json" },
      body: JSON.stringify({ token_id: "x", token_secret: "y" }),
    }),
  );
  assert(
    response.status === 422,
    "short credential values should return HTTP 422",
  );
});

Deno.test("GPU launch requires a token and rejects unsupported options before Modal RPC", async () => {
  const unauthorized = await handleRequest(
    new Request("https://studio.test/api/sessions", {
      method: "POST",
      headers: { ...sameOriginHeaders, "content-type": "application/json" },
      body: JSON.stringify({
        gpu: "T4",
        cpus: 4,
        memory_gib: 32,
        max_hours: 1,
        idle_timeout_minutes: 15,
      }),
    }),
  );
  assert(
    unauthorized.status === 401,
    "launch should require the user's Modal token",
  );
  const invalid = await handleRequest(
    new Request("https://studio.test/api/sessions", {
      method: "POST",
      headers: { ...modalHeaders, "content-type": "application/json" },
      body: JSON.stringify({
        gpu: "made-up-gpu",
        cpus: 4,
        memory_gib: 32,
        max_hours: 1,
        idle_timeout_minutes: 15,
      }),
    }),
  );
  assert(invalid.status === 422, "unsupported GPU should be rejected locally");
});

Deno.test("dataset upload without a session does not create a Modal Sandbox", async () => {
  const form = new FormData();
  form.set("file", new File(["tiny"], "tiny.csv", { type: "text/csv" }));
  const response = await handleRequest(
    new Request("https://studio.test/api/datasets", {
      method: "POST",
      headers: modalHeaders,
      body: form,
    }),
  );
  assert(
    response.status === 409,
    "upload should require an existing notebook session",
  );
});

Deno.test("cross-origin API calls are refused", async () => {
  const response = await handleRequest(
    new Request("https://studio.test/api/dashboard", {
      headers: { origin: "https://attacker.test" },
    }),
  );
  assert(response.status === 403, "cross-origin requests should be refused");
});
