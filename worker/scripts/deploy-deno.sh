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
if [[ -z "$DEPLOY_ORG" ]]; then
  echo "Set DENO_DEPLOY_ORG to the slug of your Deno Deploy organization." >&2
  exit 2
fi

cd "$ROOT/worker"
npm run sync:assets
args=(--config "$ROOT/worker/deno.json" --org "$DEPLOY_ORG" --app "$DEPLOY_APP" --prod)
"$ROOT/worker/scripts/with-deno-keyring.sh" "$DENO_BIN" deploy "${args[@]}"
