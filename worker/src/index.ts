interface Env {
  ASSETS: Fetcher;
}

function jsonError(message: string, status: number): Response {
  return Response.json({ detail: message }, { status });
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/health") return Response.json({ status: "ok" });
    if (url.pathname.startsWith("/api/")) {
      return jsonError(
        "Notebook Studio keeps its controller on your device. Start it with ./run.sh, then connect from this page.",
        410,
      );
    }
    return env.ASSETS.fetch(request);
  },
};
