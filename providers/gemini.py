from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# Code Assist backend (what the gemini CLI OAuth token is authorized for).
# Note: raw generativelanguage.googleapis.com rejects these tokens with
# ACCESS_TOKEN_SCOPE_INSUFFICIENT — the token is scoped to the Code Assist
# product, which proxies Gemini models.
_GEMINI_API_BASE = "https://cloudcode-pa.googleapis.com"
_DEFAULT_AGY_HOME = Path.home() / ".gemini" / "antigravity-cli"
_DEFAULT_CREDS_FILE = Path.home() / ".gemini" / "oauth_creds.json"

# The gemini CLI ships a public installed-app OAuth client pair in cleartext
# (@google/gemini-cli-core, code_assist/oauth2.js, with a comment noting the
# secret is not a secret). Refresh tokens obtained via `gemini` login are bound
# to THAT client — a different pair yields invalid_client/invalid_grant — so we
# need it as a last-resort fallback when the creds file omits it.
#
# It is deliberately NOT vendored into this repo: a literal GOCSPX- string trips
# every credential scanner in existence and would age badly when Google rotates
# it. Instead we read the pair back out of the CLI the user already has
# installed, which is both always current and self-evidently not our secret.

_CLIENT_ID_RE = re.compile(r"\d{10,14}-[a-z0-9]{32}\.apps\.googleusercontent\.com")
_CLIENT_SECRET_RE = re.compile(r"GOCSPX-[A-Za-z0-9_\-]{28}")

_AGY_EXE = Path(os.environ.get("AGY_EXE", "")) if os.environ.get("AGY_EXE") else (
    Path.home() / ".gemini" / "bin" / "agy.exe"
)

# Resolved at most once per process; the scan reads a ~200MB binary.
_cli_pairs_cache: list[tuple[str, str]] | None = None


def _discover_cli_client_pairs() -> list[tuple[str, str]]:
    """
    Recover the gemini CLI's built-in OAuth client pair(s) from the local install.

    Checks GEMINI_CLI_CLIENT_ID / GEMINI_CLI_CLIENT_SECRET first, then scans the
    agy binary. The binary embeds several client ids and secrets and there is no
    reliable way to tell from the bytes alone which id goes with which secret, so
    every combination is returned; the refresh loop already tries candidates in
    order and only a matching pair is accepted by Google's token endpoint.

    Returns an empty list when nothing is available, in which case the creds file
    must supply the pair itself.
    """
    global _cli_pairs_cache
    if _cli_pairs_cache is not None:
        return _cli_pairs_cache

    env_id = os.environ.get("GEMINI_CLI_CLIENT_ID", "")
    env_secret = os.environ.get("GEMINI_CLI_CLIENT_SECRET", "")
    if env_id and env_secret:
        _cli_pairs_cache = [(env_id, env_secret)]
        return _cli_pairs_cache

    pairs: list[tuple[str, str]] = []
    try:
        if _AGY_EXE.exists():
            text = _AGY_EXE.read_bytes().decode("latin-1", errors="ignore")
            ids = list(dict.fromkeys(_CLIENT_ID_RE.findall(text)))
            secrets = list(dict.fromkeys(_CLIENT_SECRET_RE.findall(text)))
            pairs = [(cid, sec) for cid in ids for sec in secrets]
    except OSError:
        pairs = []

    _cli_pairs_cache = pairs
    return pairs


class GeminiProvider:
    """
    Uses the gemini CLI (Google Code Assist) OAuth credentials to probe the
    Gemini quota as the signed-in Google account — no API key, no billing.

    - The canary call is cloudcode-pa.googleapis.com/v1internal:countTokens;
      a 429 response carries the exhausted window (RPM vs RPD).
    - Access token is auto-refreshed via the stored refresh_token. The
      refresh grant uses the pair in the creds file when present, otherwise
      the gemini CLI's public installed-app pair, recovered from the local
      install (see _discover_cli_client_pairs) — so `gemini` re-logins that
      rewrite the creds file do not break refresh.
    - Usage percentages (RPM/RPD) are read from history.jsonl when accessible.
    - Setup: log in with the `gemini` CLI (security.auth.selectedType =
      "oauth-personal"). No manual credential merging required.
    """

    def __init__(
        self,
        creds_file: str = "",
        history_file: str = "",
        model: str = "gemini-2.0-flash",
        daily_limit: int = 1000,
        rpm_limit: int = 15,
        timeout_seconds: int = 15,
        client_id: str = "",
        client_secret: str = "",
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        resolved_creds = creds_file or os.environ.get("GEMINI_CREDS_FILE", "")
        self.creds_file = Path(resolved_creds).expanduser() if resolved_creds else _DEFAULT_CREDS_FILE

        resolved_history = history_file or os.environ.get("GEMINI_HISTORY_FILE", "")
        self.history_file = (
            Path(resolved_history).expanduser() if resolved_history
            else _DEFAULT_AGY_HOME / "history.jsonl"
        )

        self.model = model
        self.daily_limit = daily_limit
        self.rpm_limit = rpm_limit
        self.timeout_seconds = timeout_seconds

    # ── Credentials ───────────────────────────────────────────────────────────

    def _load_creds(self) -> dict[str, Any]:
        if not self.creds_file.exists():
            raise RuntimeError(
                f"Gemini OAuth credentials not found at {self.creds_file}. "
                "Run gemini-creds-setup.py on your dev machine and copy the output to this server, "
                "then set GEMINI_CREDS_FILE."
            )
        with self.creds_file.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    def _refresh_access_token(self, creds: dict[str, Any]) -> dict[str, Any]:
        refresh_token = creds.get("refresh_token")
        if not refresh_token:
            raise RuntimeError(
                "No refresh_token in credentials file — re-login with 'agy' on your dev machine."
            )
        token_uri = creds.get("token_uri", "https://oauth2.googleapis.com/token")

        # Build list of client credential pairs to try.
        # Priority: creds-file pair(s) first (may embed several), then the
        # config.json pair (providers.gemini.client_id/client_secret), then
        # the gemini CLI's built-in public pair as a last resort — the
        # credentials file is rewritten by `gemini` re-logins, so the fallback
        # keeps refresh working even when the pair is missing from it.
        pairs: list[tuple[str, str]] = []
        for p in creds.get("oauth_pairs", []):
            if p.get("client_id") and p.get("client_secret"):
                pairs.append((p["client_id"], p["client_secret"]))
        if creds.get("client_id") and creds.get("client_secret"):
            primary = (creds["client_id"], creds["client_secret"])
            if primary not in pairs:
                pairs.insert(0, primary)
        for candidate in [(self.client_id, self.client_secret), *_discover_cli_client_pairs()]:
            if candidate[0] and candidate[1] and candidate not in pairs:
                pairs.append(candidate)
        if not pairs:
            raise RuntimeError(
                "client_id/client_secret missing. Re-run gemini-creds-setup.py and copy the output."
            )

        last_exc: Exception | None = None
        new_tokens: dict[str, Any] | None = None
        for client_id, client_secret in pairs:
            payload = urlencode({
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            }).encode()
            req = Request(token_uri, data=payload, method="POST",
                          headers={"content-type": "application/x-www-form-urlencoded"})
            try:
                with urlopen(req, timeout=self.timeout_seconds) as resp:
                    new_tokens = json.load(resp)
                # Success — promote this pair to primary
                creds["client_id"] = client_id
                creds["client_secret"] = client_secret
                break
            except HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                last_exc = RuntimeError(f"Token refresh failed (HTTP {exc.code}): {body[:200]}")
            except URLError as exc:
                last_exc = RuntimeError(f"Token refresh connection error: {exc}")

        if new_tokens is None:
            raise last_exc or RuntimeError("Token refresh failed with all credential pairs")

        creds["access_token"] = new_tokens["access_token"]
        creds["expiry_date"] = int((time.time() + new_tokens["expires_in"]) * 1000)

        try:
            with self.creds_file.open("w", encoding="utf-8") as fh:
                json.dump(creds, fh, indent=2)
        except OSError:
            pass  # read-only deployment — token still works this session

        return creds

    def _get_access_token(self) -> str:
        creds = self._load_creds()
        if time.time() * 1000 + 60_000 >= creds.get("expiry_date", 0):
            creds = self._refresh_access_token(creds)
        return creds["access_token"]

    # ── History counting ──────────────────────────────────────────────────────

    def _count_from_history(self) -> tuple[int | None, int | None]:
        """Parse history.jsonl to get (daily_used, rpm_used). Returns (None, None) if unavailable."""
        if not self.history_file.exists():
            return None, None

        now_ms = time.time() * 1000
        today_start_ms = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp() * 1000
        minute_start_ms = now_ms - 60_000

        daily, rpm = 0, 0
        try:
            with self.history_file.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = entry.get("timestamp", 0)
                    display = entry.get("display", "")
                    if not display or display.startswith("/"):
                        continue
                    if ts >= today_start_ms:
                        daily += 1
                    if ts >= minute_start_ms:
                        rpm += 1
        except OSError:
            return None, None

        return daily, rpm

    # ── Window builders ───────────────────────────────────────────────────────

    def _make_window(
        self, label: str, used: int | None, limit: int, duration_mins: int
    ) -> dict[str, Any]:
        if used is None:
            return {
                "label": label, "usedPercent": None, "remainingPercent": None,
                "windowDurationMins": duration_mins, "resetsAt": None,
            }
        used_pct = round(min(100, used / limit * 100), 1) if limit > 0 else None
        remaining_pct = max(0, 100 - int(used_pct)) if used_pct is not None else None
        return {
            "label": label, "usedPercent": used_pct, "remainingPercent": remaining_pct,
            "windowDurationMins": duration_mins, "resetsAt": None,
        }

    @staticmethod
    def _exhausted_window(label: str, resets_at: int | None, duration_mins: int) -> dict[str, Any]:
        return {
            "label": label, "usedPercent": 100, "remainingPercent": 0,
            "windowDurationMins": duration_mins, "resetsAt": resets_at,
        }

    # ── API probe ─────────────────────────────────────────────────────────────

    def _count_tokens(self, access_token: str) -> None:
        # Code Assist API: v1internal:countTokens with a nested `request` body
        # (model prefixed with "models/"), per @google/gemini-cli-core converter.js.
        req = Request(
            f"{_GEMINI_API_BASE}/v1internal:countTokens",
            data=json.dumps({
                "request": {
                    "model": f"models/{self.model}",
                    "contents": [{"role": "user", "parts": [{"text": "x"}]}],
                },
            }).encode(),
            headers={
                "authorization": f"Bearer {access_token}",
                "content-type": "application/json",
                "user-agent": "coding-limits-gateway/0.3.0",
            },
            method="POST",
        )
        try:
            with urlopen(req, timeout=self.timeout_seconds) as resp:
                resp.read()
        except HTTPError as exc:
            raise exc
        except URLError as exc:
            raise RuntimeError(f"gemini connection error: {exc}") from exc

    def _parse_rate_limit_error(self, exc: HTTPError) -> dict[str, Any]:
        retry_after_raw = exc.headers.get("Retry-After")
        resets_at = int(time.time()) + int(retry_after_raw) if retry_after_raw else None
        is_minute = False
        try:
            body = json.loads(exc.read().decode("utf-8", errors="replace"))
            error = body.get("error", {})
            details = error.get("details", [])
            quota_id = next(
                (d.get("metadata", {}).get("quota_id") or d.get("metadata", {}).get("quotaId")
                 for d in details if d.get("metadata")),
                None,
            )
            # Code Assist 429s may not carry quota_id metadata; the message is
            # shaped like: Quota exceeded ... limit 'RequestsPerDay' of service ...
            message = error.get("message", "")
            if quota_id and "PerMinute" in quota_id:
                is_minute = True
            elif "RequestsPerMinute" in message or "PerMinute" in message:
                is_minute = True
        except Exception:
            is_minute = False

        short = self._exhausted_window("RPM", resets_at, 1) if is_minute else self._make_window("RPM", None, self.rpm_limit, 1)
        long_ = self._make_window("RPD", None, self.daily_limit, 1440) if is_minute else self._exhausted_window("RPD", resets_at, 1440)
        return {
            "enabled": True, "ok": False, "source": "gemini-oauth",
            "planType": "personal", "limitId": "gemini",
            "shortWindow": short, "longWindow": long_,
            "credits": None,
            "rateLimitReachedType": "rpm" if is_minute else "rpd",
            "error": "Rate limit exceeded (" + ("RPM" if is_minute else "RPD") + ")."
                     + (f" Resets in {retry_after_raw}s." if retry_after_raw else ""),
        }

    # ── Public API ────────────────────────────────────────────────────────────

    def fetch(self) -> dict[str, Any]:
        access_token = self._get_access_token()

        try:
            self._count_tokens(access_token)
        except HTTPError as exc:
            if exc.code == 429:
                return self._parse_rate_limit_error(exc)
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"gemini HTTP {exc.code}: {body[:200]}") from exc

        daily_used, rpm_used = self._count_from_history()

        return {
            "enabled": True,
            "ok": True,
            "source": "gemini-oauth",
            "planType": "personal",
            "limitId": "gemini",
            "shortWindow": self._make_window("RPM", rpm_used, self.rpm_limit, 1),
            "longWindow": self._make_window("RPD", daily_used, self.daily_limit, 1440),
            "credits": None,
            "rateLimitReachedType": None,
            "error": None,
        }
