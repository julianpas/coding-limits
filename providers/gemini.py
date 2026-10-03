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

# Code Assist backend. The Antigravity free tier exposes real usage via
# v1internal:retrieveUserQuotaSummary — a weekly limit and a rolling 5-hour
# limit per model group, each as a remainingFraction with a resetTime.
# The endpoint licenses by OAuth client *and* user-agent: it only accepts a
# token minted for the Antigravity client (see oauth_pairs, written by
# export-gemini-creds.py) sent with the Antigravity CLI user-agent.
_GEMINI_API_BASE = "https://cloudcode-pa.googleapis.com"
_QUOTA_ENDPOINT = "/v1internal:retrieveUserQuotaSummary"
_ANTIGRAVITY_UA = (
    "antigravity/cli/1.2.16 (aidev_client; os_type=windows; arch=amd64; auth_method=consumer)"
)
# The consumer/free-tier project the quota call is scoped to (same for every
# individual account); overridable via GEMINI_PROJECT.
_CONSUMER_PROJECT = "aicode-consumers"
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
    Reads the real Antigravity usage quota as the signed-in Google account —
    no API key, no billing.

    - The data source is cloudcode-pa.googleapis.com/v1internal:
      retrieveUserQuotaSummary, which reports the live weekly and rolling
      5-hour limits for the Gemini model group as remainingFraction + resetTime
      (this is exactly what the Antigravity IDE shows).
    - The call must use the Antigravity OAuth credentials (oauth_pairs written
      by export-gemini-creds.py) sent with the Antigravity CLI user-agent;
      a plain gemini-CLI login is rejected with 403 PERMISSION_DENIED.
    - Access token is auto-refreshed via the stored refresh_token, trying the
      creds-file pair(s) first, then the gemini CLI's public installed-app
      pair recovered from the local install (see _discover_cli_client_pairs).
    - Setup (once): run export-gemini-creds.py after logging in with Antigravity
      so oauth_creds.json carries the Antigravity refresh token + oauth_pairs.
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
        project: str = "",
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        resolved_creds = creds_file or os.environ.get("GEMINI_CREDS_FILE", "")
        self.creds_file = Path(resolved_creds).expanduser() if resolved_creds else _DEFAULT_CREDS_FILE

        self.project = project or os.environ.get("GEMINI_PROJECT", "") or _CONSUMER_PROJECT

        # Retained for config/back-compat; not used by the quota-summary path.
        self.model = model
        self.daily_limit = daily_limit
        self.rpm_limit = rpm_limit
        self.timeout_seconds = timeout_seconds

    # ── Credentials ───────────────────────────────────────────────────────────

    def _load_creds(self) -> dict[str, Any]:
        if not self.creds_file.exists():
            raise RuntimeError(
                f"Gemini OAuth credentials not found at {self.creds_file}. "
                "Run export-gemini-creds.py (after an Antigravity login) to write it, "
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

    # ── Quota summary ─────────────────────────────────────────────────────────

    def _retrieve_quota_summary(self, access_token: str) -> dict[str, Any]:
        """
        Call the Code Assist quota API as the Antigravity CLI does.

        The endpoint licenses by OAuth client *and* user-agent: only a token
        minted for the Antigravity client (the oauth_pairs written by
        export-gemini-creds.py) combined with the Antigravity CLI user-agent is
        authorized. A gemini-CLI token, or a generic user-agent, is rejected
        with 403 PERMISSION_DENIED / UNSUPPORTED_CLIENT.
        """
        req = Request(
            f"{_GEMINI_API_BASE}{_QUOTA_ENDPOINT}",
            data=json.dumps({"project": self.project}).encode(),
            headers={
                "authorization": f"Bearer {access_token}",
                "content-type": "application/json",
                "user-agent": _ANTIGRAVITY_UA,
            },
            method="POST",
        )
        try:
            with urlopen(req, timeout=self.timeout_seconds) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code == 403:
                raise RuntimeError(
                    "gemini quota API returned 403 PERMISSION_DENIED — the token is not "
                    "authorized for Antigravity. Re-run export-gemini-creds.py so "
                    f"{self.creds_file} carries the Antigravity credentials. Body: {body[:200]}"
                ) from exc
            raise RuntimeError(f"gemini quota HTTP {exc.code}: {body[:200]}") from exc
        except URLError as exc:
            raise RuntimeError(f"gemini connection error: {exc}") from exc

    # ── Window builders ───────────────────────────────────────────────────────

    @staticmethod
    def _gemini_group(summary: dict[str, Any]) -> dict[str, Any]:
        """Return the model group that holds the Gemini buckets (Flash/Pro)."""
        groups = summary.get("groups", []) if isinstance(summary, dict) else []
        for group in groups:
            for bucket in group.get("buckets", []):
                if str(bucket.get("bucketId", "")).startswith("gemini"):
                    return group
        return groups[0] if groups else {}

    @staticmethod
    def _bucket(group: dict[str, Any], window: str) -> dict[str, Any] | None:
        buckets = group.get("buckets", [])
        for bucket in buckets:
            if bucket.get("window") == window and str(bucket.get("bucketId", "")).startswith("gemini"):
                return bucket
        for bucket in buckets:  # fall back to any bucket for this window
            if bucket.get("window") == window:
                return bucket
        return None

    @staticmethod
    def _window_from_bucket(
        bucket: dict[str, Any] | None, label: str, duration_mins: int
    ) -> dict[str, Any]:
        if not bucket:
            return {
                "label": label, "usedPercent": None, "remainingPercent": None,
                "windowDurationMins": duration_mins, "resetsAt": None,
            }

        frac = bucket.get("remainingFraction")
        if frac is None:
            used_pct = remaining_pct = None
        else:
            remaining_pct = max(0.0, min(100.0, round(float(frac) * 100, 1)))
            used_pct = round(100.0 - remaining_pct, 1)

        reset_epoch = None
        reset_time = bucket.get("resetTime")
        if reset_time:
            try:
                dt = datetime.fromisoformat(str(reset_time).replace("Z", "+00:00"))
                reset_epoch = int(dt.astimezone(timezone.utc).timestamp())
            except ValueError:
                reset_epoch = None

        return {
            "label": label, "usedPercent": used_pct, "remainingPercent": remaining_pct,
            "windowDurationMins": duration_mins, "resetsAt": reset_epoch,
        }

    # ── Public API ────────────────────────────────────────────────────────────

    def fetch(self) -> dict[str, Any]:
        access_token = self._get_access_token()
        summary = self._retrieve_quota_summary(access_token)

        group = self._gemini_group(summary)
        short = self._window_from_bucket(self._bucket(group, "5h"), "5h", 300)
        long_ = self._window_from_bucket(self._bucket(group, "weekly"), "Weekly", 10080)

        rate_type = None
        if short["remainingPercent"] == 0:
            rate_type = "5h"
        elif long_["remainingPercent"] == 0:
            rate_type = "weekly"

        return {
            "enabled": True,
            "ok": True,
            "source": "gemini-oauth",
            "planType": "personal",
            "limitId": "gemini",
            "shortWindow": short,
            "longWindow": long_,
            "credits": None,
            "rateLimitReachedType": rate_type,
            "error": None,
        }
