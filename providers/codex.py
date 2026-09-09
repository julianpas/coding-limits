from __future__ import annotations

import json
import shutil
import subprocess
import time
from typing import Any


def limit_window(label: str, raw: dict[str, Any]) -> dict[str, Any]:
    used = raw.get("usedPercent")
    remaining = None if used is None else max(0, min(100, 100 - int(used)))
    return {
        "label": label,
        "usedPercent": used,
        "remainingPercent": remaining,
        "windowDurationMins": raw.get("windowDurationMins"),
        "resetsAt": raw.get("resetsAt"),
    }


class CodexProvider:
    def __init__(self, codex_path: str = "codex", timeout_seconds: int = 10) -> None:
        self.codex_path = codex_path
        self.timeout_seconds = timeout_seconds

    def fetch(self) -> dict[str, Any]:
        if not shutil.which(self.codex_path):
            raise RuntimeError(
                f"codex binary not found at {self.codex_path!r}. "
                "Install it and ensure it is in PATH, or set CODEX_PATH."
            )

        init_request = {
            "id": "init",
            "method": "initialize",
            "params": {
                "clientInfo": {"name": "codex-limits-gateway", "version": "0.2.0"},
                "capabilities": {"experimentalApi": True},
            },
        }
        limits_request = {
            "id": "limits",
            "method": "account/rateLimits/read",
            "params": None,
        }
        payload = json.dumps(init_request) + "\n" + json.dumps(limits_request) + "\n"

        process = subprocess.Popen(
            [self.codex_path, "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            # codex.cmd is a console app: without this, a console-less parent
            # (pythonw tray app) makes Windows pop a new console window on
            # every fetch. NO_WINDOW suppresses it while pipes still work.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        try:
            if process.stdin is None or process.stdout is None:
                raise RuntimeError("failed to open codex app-server stdin/stdout")

            process.stdin.write(payload)
            process.stdin.flush()

            parsed: dict[str, Any] | None = None
            deadline = time.monotonic() + self.timeout_seconds
            while time.monotonic() < deadline:
                line = process.stdout.readline()
                if not line:
                    time.sleep(0.05)
                    continue
                try:
                    message = json.loads(line.strip())
                except json.JSONDecodeError:
                    continue
                if message.get("id") == "limits" and "result" in message:
                    parsed = message["result"]
                    break

            if parsed is None:
                stderr = ""
                if process.stderr is not None:
                    stderr = process.stderr.read().strip()
                raise RuntimeError(
                    stderr
                    or f"codex app-server returned no result within {self.timeout_seconds}s"
                )
        finally:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
            for attr in ("stdin", "stdout", "stderr"):
                stream = getattr(process, attr, None)
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass

        snapshot = (
            parsed.get("rateLimitsByLimitId", {}).get("codex")
            or parsed.get("rateLimits")
            or {}
        )
        primary = snapshot.get("primary") or {}
        secondary = snapshot.get("secondary") or {}
        credits = snapshot.get("credits") or {}

        return {
            "enabled": True,
            "ok": True,
            "source": "codex-app-server",
            "planType": snapshot.get("planType"),
            "limitId": snapshot.get("limitId"),
            "shortWindow": limit_window("5h", primary),
            "longWindow": limit_window("7d", secondary),
            "credits": {
                "hasCredits": credits.get("hasCredits"),
                "unlimited": credits.get("unlimited"),
                "balance": credits.get("balance"),
            },
            "rateLimitReachedType": snapshot.get("rateLimitReachedType"),
            "error": None,
        }
