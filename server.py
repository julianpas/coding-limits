#!/usr/bin/env python3
from __future__ import annotations

import hmac
import json
import logging
import os
import time
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from providers import ClaudeProvider, CodexProvider, GeminiProvider

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("CODEX_LIMITS_CONFIG", ROOT / "config.json"))
VERSION = "0.3.0"

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("coding-limits")

# Per-provider cache: { name: {"data": dict, "ts": float} }
_cache: dict[str, dict[str, Any]] = {}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_config() -> dict[str, Any]:
    config: dict[str, Any] = {
        "listen_host": "0.0.0.0",
        "listen_port": 8765,
        "device_name": "AI Limits",
        "access_token": "",
        "cache_ttl_seconds": 300,
        "providers": {
            "codex": {
                "enabled": True,
                "codex_path": "codex",
                "timeout_seconds": 10,
            },
            "claude": {
                "enabled": False,
                "base_url": "https://claude.ai",
                "session_key": "",
                "timeout_seconds": 15,
            },
            "gemini": {
                "enabled": False,
                "creds_file": "",
                "history_file": "",
                "model": "gemini-2.0-flash",
                "daily_limit": 1000,
                "rpm_limit": 15,
                "timeout_seconds": 15,
                "client_id": "",
                "client_secret": "",
            },
        },
    }

    if CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open("r", encoding="utf-8") as fh:
                file_config = json.load(fh)
            config = deep_merge(config, file_config)
        except json.JSONDecodeError as exc:
            log.error("config.json is invalid JSON: %s — using defaults", exc)

    apply_env_overrides(config)
    validate_config(config)
    return config


def validate_config(config: dict[str, Any]) -> None:
    port = config.get("listen_port")
    if not isinstance(port, int) or not (1 <= port <= 65535):
        raise ValueError(f"listen_port must be 1–65535, got {port!r}")

    codex_cfg = config["providers"]["codex"]
    if codex_cfg.get("enabled"):
        codex_path = codex_cfg.get("codex_path", "codex")
        import shutil
        if not shutil.which(codex_path):
            log.warning(
                "codex binary %r not found in PATH — Codex provider will fail at runtime",
                codex_path,
            )

    claude_cfg = config["providers"]["claude"]
    if claude_cfg.get("enabled") and not claude_cfg.get("session_key"):
        log.warning("CLAUDE_ENABLED=true but CLAUDE_SESSION_KEY is empty — Claude provider will fail")

    gemini_cfg = config["providers"]["gemini"]
    if gemini_cfg.get("enabled") and not gemini_cfg.get("creds_file"):
        from pathlib import Path as _Path
        default = _Path.home() / ".gemini" / "oauth_creds.json"
        if not default.exists():
            log.warning(
                "GEMINI_ENABLED=true but no credentials found at %s — "
                "copy ~/.gemini/oauth_creds.json from your dev machine or set GEMINI_CREDS_FILE",
                default,
            )


def deep_merge(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def parse_env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    try:
        return int(value)
    except ValueError:
        log.warning("Invalid value for %s: %r — using default %d", name, value, default)
        return default


def apply_env_overrides(config: dict[str, Any]) -> None:
    config["listen_host"] = os.environ.get("LISTEN_HOST", config["listen_host"])
    config["listen_port"] = parse_env_int("LISTEN_PORT", int(config["listen_port"]))
    config["device_name"] = os.environ.get("DEVICE_NAME", config["device_name"])
    config["access_token"] = os.environ.get("ACCESS_TOKEN", config["access_token"])
    config["cache_ttl_seconds"] = parse_env_int("CACHE_TTL_SECONDS", int(config["cache_ttl_seconds"]))

    codex_cfg = config["providers"]["codex"]
    codex_cfg["enabled"] = parse_env_bool("CODEX_ENABLED", bool(codex_cfg["enabled"]))
    codex_cfg["codex_path"] = os.environ.get("CODEX_PATH", codex_cfg["codex_path"])
    codex_cfg["timeout_seconds"] = parse_env_int("CODEX_TIMEOUT_SECONDS", int(codex_cfg["timeout_seconds"]))

    claude_cfg = config["providers"]["claude"]
    claude_cfg["enabled"] = parse_env_bool("CLAUDE_ENABLED", bool(claude_cfg["enabled"]))
    claude_cfg["base_url"] = os.environ.get("CLAUDE_BASE_URL", claude_cfg["base_url"])
    claude_cfg["session_key"] = os.environ.get("CLAUDE_SESSION_KEY", claude_cfg["session_key"])
    claude_cfg["timeout_seconds"] = parse_env_int("CLAUDE_TIMEOUT_SECONDS", int(claude_cfg["timeout_seconds"]))

    gemini_cfg = config["providers"]["gemini"]
    gemini_cfg["enabled"] = parse_env_bool("GEMINI_ENABLED", bool(gemini_cfg["enabled"]))
    gemini_cfg["creds_file"] = os.environ.get("GEMINI_CREDS_FILE", gemini_cfg["creds_file"])
    gemini_cfg["history_file"] = os.environ.get("GEMINI_HISTORY_FILE", gemini_cfg["history_file"])
    gemini_cfg["model"] = os.environ.get("GEMINI_MODEL", gemini_cfg["model"])
    gemini_cfg["daily_limit"] = parse_env_int("GEMINI_DAILY_LIMIT", int(gemini_cfg["daily_limit"]))
    gemini_cfg["rpm_limit"] = parse_env_int("GEMINI_RPM_LIMIT", int(gemini_cfg["rpm_limit"]))
    gemini_cfg["timeout_seconds"] = parse_env_int("GEMINI_TIMEOUT_SECONDS", int(gemini_cfg["timeout_seconds"]))


def fetch_with_retry(provider: Any, name: str, max_attempts: int = 2) -> dict[str, Any]:
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return provider.fetch()
        except Exception as exc:
            last_exc = exc
            if attempt < max_attempts:
                log.warning("%s fetch attempt %d failed: %s — retrying", name, attempt, exc)
                time.sleep(1.0)
    raise last_exc  # type: ignore[misc]


def build_snapshot(config: dict[str, Any]) -> dict[str, Any]:
    providers_cfg = config.get("providers", {})
    cache_ttl = float(config.get("cache_ttl_seconds", 300))

    snapshot: dict[str, Any] = {
        "ok": True,
        "fetchedAt": now_iso(),
        "version": VERSION,
        "deviceName": config.get("device_name", "AI Limits"),
        "providers": {},
    }

    provider_specs = [
        (
            "codex",
            "codex-app-server",
            CodexProvider,
            {
                "codex_path": providers_cfg.get("codex", {}).get("codex_path", "codex"),
                "timeout_seconds": int(providers_cfg.get("codex", {}).get("timeout_seconds", 10)),
            },
        ),
        (
            "claude",
            "claude-web",
            ClaudeProvider,
            {
                "base_url": providers_cfg.get("claude", {}).get("base_url", "https://claude.ai"),
                "session_key": providers_cfg.get("claude", {}).get("session_key", ""),
                "timeout_seconds": int(providers_cfg.get("claude", {}).get("timeout_seconds", 15)),
            },
        ),
        (
            "gemini",
            "gemini-oauth",
            GeminiProvider,
            {
                "creds_file": providers_cfg.get("gemini", {}).get("creds_file", ""),
                "history_file": providers_cfg.get("gemini", {}).get("history_file", ""),
                "model": providers_cfg.get("gemini", {}).get("model", "gemini-2.0-flash"),
                "daily_limit": int(providers_cfg.get("gemini", {}).get("daily_limit", 1000)),
                "rpm_limit": int(providers_cfg.get("gemini", {}).get("rpm_limit", 15)),
                "timeout_seconds": int(providers_cfg.get("gemini", {}).get("timeout_seconds", 15)),
                "client_id": providers_cfg.get("gemini", {}).get("client_id", ""),
                "client_secret": providers_cfg.get("gemini", {}).get("client_secret", ""),
            },
        ),
    ]

    for name, source_label, provider_cls, extra_kwargs in provider_specs:
        cfg = providers_cfg.get(name, {})
        if not cfg.get("enabled", False):
            snapshot["providers"][name] = {"enabled": False, "ok": False, "source": "disabled", "error": None}
            continue

        try:
            provider = provider_cls(**extra_kwargs)
            data = fetch_with_retry(provider, name)
            _cache[name] = {"data": data, "ts": time.monotonic()}
            snapshot["providers"][name] = data
            log.info("%s: fetch ok", name)
        except Exception as exc:
            traceback.print_exc()
            log.error("%s: fetch failed: %s", name, exc)
            snapshot["ok"] = False

            cached = _cache.get(name)
            if cached and (time.monotonic() - cached["ts"]) < cache_ttl:
                stale = dict(cached["data"])
                stale["stale"] = True
                stale["error"] = f"[stale] {exc}"
                snapshot["providers"][name] = stale
                log.info("%s: serving cached data (age %.0fs)", name, time.monotonic() - cached["ts"])
            else:
                snapshot["providers"][name] = {
                    "enabled": True,
                    "ok": False,
                    "source": source_label,
                    "error": str(exc),
                }

    return snapshot


class Handler(BaseHTTPRequestHandler):
    server_version = f"coding-limits-gateway/{VERSION}"

    def _token_valid(self, config: dict[str, Any]) -> bool:
        expected = config.get("access_token", "")
        if not expected:
            return True
        provided = self.headers.get("X-Gauge-Token", "")
        return hmac.compare_digest(provided.encode(), expected.encode())

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_dashboard(self) -> None:
        page = ROOT / "dashboard.html"
        if not page.exists():
            self._send_json(404, {"ok": False, "error": "dashboard.html not found"})
            return
        encoded = page.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802
        config = load_config()
        path = urlparse(self.path).path

        if path in ("/", "/index.html", "/dashboard.html"):
            self._send_dashboard()
            return

        if path == "/health":
            health: dict[str, Any] = {
                "ok": True,
                "time": now_iso(),
                "version": VERSION,
                "providers": {},
            }
            for name in ("codex", "claude", "gemini"):
                cfg = config["providers"].get(name, {})
                cached = _cache.get(name)
                health["providers"][name] = {
                    "enabled": bool(cfg.get("enabled")),
                    "ok": cached["data"].get("ok") if cached else None,
                    "lastFetchAt": datetime.fromtimestamp(
                        cached["ts"], tz=timezone.utc
                    ).isoformat().replace("+00:00", "Z") if cached else None,
                    "stale": (time.monotonic() - cached["ts"]) > float(config.get("cache_ttl_seconds", 300)) if cached else None,
                }
            self._send_json(200, health)
            return

        if not self._token_valid(config):
            self._send_json(401, {"ok": False, "error": "missing or invalid X-Gauge-Token"})
            return

        if path == "/api/v1/snapshot":
            try:
                self._send_json(200, build_snapshot(config))
            except Exception as exc:
                traceback.print_exc()
                self._send_json(500, {"ok": False, "error": str(exc)})
            return

        self._send_json(404, {"ok": False, "error": "not found"})

    def log_message(self, fmt: str, *args: Any) -> None:
        log.info("%s - %s", self.address_string(), fmt % args)


def main() -> None:
    config = load_config()
    host = config.get("listen_host", "0.0.0.0")
    port = int(config.get("listen_port", 8765))
    server = ThreadingHTTPServer((host, port), Handler)
    log.info("coding-limits gateway v%s listening on http://%s:%d", VERSION, host, port)
    log.info("config: %s", CONFIG_PATH)
    server.serve_forever()


if __name__ == "__main__":
    main()
