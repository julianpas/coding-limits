#!/usr/bin/env python3
r"""
Prepare ~/.gemini/oauth_creds.json so the coding-limits gateway can refresh
Gemini access tokens on its own.

Windows-friendly alternative to gemini-creds-setup.py: it extracts the OAuth
client pairs directly from the local agy.exe (no `strings` binary, no PATH
requirement) and merges them into the credentials file created by an
interactive `agy` (or `gemini`) login.

Usage:
    1. Log in once (interactive):  & "$env:USERPROFILE\.gemini\bin\agy.exe"
       (the `gemini` CLI login flow works too and writes the same file;
        it already embeds client credentials, in which case step 2 is a no-op)
    2. python prepare-gemini-creds.py
    3. Ensure providers.gemini.enabled = true in config.json
    4. curl http://localhost:8765/api/v1/snapshot
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

CREDS_PATH = Path(
    os.environ.get("GEMINI_CREDS_FILE", str(Path.home() / ".gemini" / "oauth_creds.json"))
)
AGY_EXE = Path(os.environ.get("AGY_EXE", str(Path.home() / ".gemini" / "bin" / "agy.exe")))


def _extract_pairs(exe: Path) -> list[dict[str, str]]:
    """Return all (client_id, client_secret) pairs found in the agy binary."""
    text = exe.read_bytes().decode("latin-1", errors="ignore")
    client_ids = re.findall(r"\d{10,14}-[a-z0-9]{32}\.apps\.googleusercontent\.com", text)
    secrets = re.findall(r"GOCSPX-[A-Za-z0-9_\-]{28}", text)
    if not client_ids or not secrets:
        return []
    pairs = list(zip(dict.fromkeys(client_ids), dict.fromkeys(secrets)))
    return [{"client_id": cid, "client_secret": sec} for cid, sec in pairs]


def main() -> int:
    if not CREDS_PATH.exists():
        print(f"Error: no credentials at {CREDS_PATH}", file=sys.stderr)
        print("Log in first (interactive, one-time):", file=sys.stderr)
        print(f'    & "{AGY_EXE}"', file=sys.stderr)
        print("  or use the `gemini` CLI login flow, then re-run this script.", file=sys.stderr)
        return 1

    creds = json.loads(CREDS_PATH.read_text(encoding="utf-8"))

    if creds.get("client_id") and creds.get("client_secret"):
        print(f"OK: {CREDS_PATH} already carries client_id/client_secret — nothing to do.")
        return 0

    pairs = _extract_pairs(AGY_EXE) if AGY_EXE.exists() else []
    if not pairs:
        env_id = os.environ.get("GEMINI_CLIENT_ID", "")
        env_secret = os.environ.get("GEMINI_CLIENT_SECRET", "")
        if env_id and env_secret:
            pairs = [{"client_id": env_id, "client_secret": env_secret}]
    if not pairs:
        print(
            "Error: no OAuth client credentials found (agy.exe missing/unreadable and "
            "GEMINI_CLIENT_ID/GEMINI_CLIENT_SECRET not set).",
            file=sys.stderr,
        )
        return 1

    creds["oauth_pairs"] = pairs
    creds["client_id"] = pairs[0]["client_id"]
    creds["client_secret"] = pairs[0]["client_secret"]
    creds["token_uri"] = creds.get("token_uri", "https://oauth2.googleapis.com/token")
    CREDS_PATH.write_text(json.dumps(creds, indent=2), encoding="utf-8")
    print(f"OK: embedded {len(pairs)} OAuth client pair(s) into {CREDS_PATH}")
    print(f"    primary client_id: {creds['client_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
