interface Env {
  ASSETS: Fetcher;
  DENO_API_ORIGIN?: string;
}

function jsonError(message: string, status: number): Response {
  return Response.json({ detail: message }, { status });
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/studio-config.js") {
      const apiOrigin = (env.DENO_API_ORIGIN || "").replace(/\/+$/, "");
      return new Response(
        "window.NOTEBOOK_STUDIO_API_BASE = " + JSON.stringify(apiOrigin) + ";\n",
        {
          headers: {
            "content-type": "text/javascript; charset=utf-8",
            "cache-control": "no-store",
            "x-content-type-options": "nosniff",
          },
        },
      );
    }
    if (url.pathname === "/health") return Response.json({ status: "ok" });
    if (url.pathname.startsWith("/api/")) {
      return jsonError(
        "API requests are served by the Deno endpoint configured for this website.",
        410,
      );
    }
    return env.ASSETS.fetch(request);
  },
};
