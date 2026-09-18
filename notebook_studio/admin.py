from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import urllib.error
import urllib.request

from .config import Settings
from .credentials import USERNAME_RE, password_hash
from .database import Database


def load_local_env() -> None:
    env_path = Path(".env")
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if not value or value.startswith("#") or "=" not in value:
            continue
        key, raw = value.split("=", 1)
        if key.strip() and key.strip() not in os.environ:
            os.environ[key.strip()] = raw.strip().strip('"').strip("'")


def main() -> None:
    load_local_env()
    parser = argparse.ArgumentParser(prog="notebook-studio-user")
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add", help="provision a login for shared mode")
    add.add_argument("username")
    add.add_argument("--server", help="create the account on a hosted Notebook Studio URL")
    args = parser.parse_args()
    if not USERNAME_RE.fullmatch(args.username):
        parser.error("username must be 2-32 letters, numbers, dots, dashes, or underscores")
    password = getpass.getpass("New password (at least 12 characters): ")
    confirm = getpass.getpass("Repeat password: ")
    if len(password) < 12:
        parser.error("password must contain at least 12 characters")
    if password != confirm:
        parser.error("passwords do not match")
    if args.server:
        admin_key = os.getenv("NOTEBOOK_STUDIO_ADMIN_KEY", "")
        if not admin_key:
            parser.error("set NOTEBOOK_STUDIO_ADMIN_KEY to the hosted admin provisioning key")
        body = json.dumps({"username": args.username, "password": password}).encode("utf-8")
        request = urllib.request.Request(
            args.server.rstrip("/") + "/api/admin/users",
            data=body,
            headers={"Content-Type": "application/json", "X-Admin-Provisioning-Key": admin_key},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            parser.error(exc.read().decode("utf-8", errors="replace"))
        print(f"Created hosted account {result['username']!r}.")
        return
    db = Database(Settings.from_env().database_path)
    try:
        db.create_user(args.username, password_hash(password))
    except Exception as exc:
        if "UNIQUE constraint failed" in str(exc):
            parser.error(f"user {args.username!r} already exists")
        raise
    print(f"Created account {args.username!r}.")


if __name__ == "__main__":
    main()
