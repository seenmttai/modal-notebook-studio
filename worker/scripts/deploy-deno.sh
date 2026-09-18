#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
DENO_BIN="${DENO_BIN:-$(command -v deno || true)}"
if [[ -z "$DENO_BIN" ]]; then
  echo "Deno is required. Install it from https://deno.com/ and retry." >&2
  exit 1
fi
DEPLOY_APP="${DENO_DEPLOY_APP:-modal-notebook-studio}"
DEPLOY_ORG="${DENO_DEPLOY_ORG:-}"

cd "$ROOT/worker"
args=(--config "$ROOT/worker/deno.json" --app "$DEPLOY_APP" --prod)
if [[ -n "$DEPLOY_ORG" ]]; then
  args+=(--org "$DEPLOY_ORG")
fi
"$ROOT/worker/scripts/with-deno-keyring.sh" "$DENO_BIN" deploy "${args[@]}"
