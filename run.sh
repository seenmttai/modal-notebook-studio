#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3.11 or newer is required." >&2
  exit 1
fi
if [ ! -d .venv ]; then python3 -m venv .venv; fi
. .venv/bin/activate
python -m pip install -e '.[dev,modal,mcp]'
if [ ! -f .env ]; then
  cp .env.example .env
  python - <<'PYGEN'
from pathlib import Path
import secrets
p = Path('.env')
s = p.read_text()
s = s.replace('APP_PASSWORD=replace-with-a-long-random-password', 'APP_PASSWORD=' + secrets.token_urlsafe(24))
s = s.replace('MODAL_CREDENTIAL_ENCRYPTION_KEY=replace-with-at-least-32-random-characters', 'MODAL_CREDENTIAL_ENCRYPTION_KEY=' + secrets.token_urlsafe(48))
p.write_text(s)
p.chmod(0o600)
PYGEN
  echo "Created .env with a private local password and encryption key. Keep it private."
fi
exec python -m notebook_studio
