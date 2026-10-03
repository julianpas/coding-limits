#!/usr/bin/env python3
r"""
One-shot export of the agy (Antigravity) OAuth token from the Windows
Credential Manager into the file the coding-limits gateway expects:
    %USERPROFILE%\.gemini\oauth_creds.json

The agy CLI stores its token in the Windows Credential Manager
(target "gemini:antigravity"), NOT in a file — that is why
prepare-gemini-creds.py finds nothing.

Run this in a normal terminal (Windows Terminal / PowerShell), NOT from
inside the DSH harness (the harness intercepts the credential API and
returns an empty struct):

    C:\Python313\python.exe C:\Projects\Tools\coding-limits\export-gemini-creds.py

The script:
  1. reads the "gemini:antigravity" credential (secret may live in the
     blob OR in named credential attributes — both are checked),
  2. parses the token JSON (tolerant of several key shapes),
  3. extracts agy's OAuth client pairs from agy.exe as fallback,
  4. writes the merged creds file in the provider format.

No network access. Stdlib only. Secrets are masked in output.
"""
from __future__ import annotations

import ctypes
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from ctypes import wintypes

# cmdkey shows the entry as "LegacyGeneric:target=gemini:antigravity" — try
# both the bare name and the full literal name (covers either display-vs-stored
# ambiguity), plus the sibling gh entry is shown the same way.
CRED_TARGETS = [
    "gemini:antigravity",
    "LegacyGeneric:target=gemini:antigravity",
]
AGY_EXE = Path.home() / ".gemini" / "bin" / "agy.exe"
OUT_FILE = Path.home() / ".gemini" / "oauth_creds.json"
CLIENT_ID_RE = re.compile(rb"\d{10,14}-[a-z0-9]{32}\.apps\.googleusercontent\.com")
CLIENT_SECRET_RE = re.compile(rb"GOCSPX-[A-Za-z0-9_\-]{28}")


# ── Keyring read ──────────────────────────────────────────────────────────────

class CREDENTIAL_ATTRIBUTE(ctypes.Structure):
    _fields_ = [
        ("ValueType", wintypes.DWORD),
        ("Name", wintypes.LPWSTR),
        ("ValueLength", wintypes.DWORD),
        ("Value", ctypes.POINTER(ctypes.c_byte)),
    ]


class CREDENTIALA(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", ctypes.c_longlong),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attribute", ctypes.POINTER(CREDENTIAL_ATTRIBUTE)),
        ("Reserved", ctypes.c_void_p),
    ]


def read_credential(target: str) -> dict[str, Any]:
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    pcred = ctypes.POINTER(CREDENTIALA)
    # CredReadW's last arg is PCREDENTIAL* — it allocates the credential and
    # writes its address into our out-param. Passing a pre-allocated struct
    # (the old bug) leaves it uninitialized, so we read garbage / a 0-byte blob.
    adv.CredReadW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(pcred)
    ]
    adv.CredReadW.restype = wintypes.BOOL
    adv.CredFree.argtypes = [ctypes.c_void_p]
    adv.CredFree.restype = None

    ptr = pcred()
    if not adv.CredReadW(target, 1, 0, ctypes.byref(ptr)):  # 1 = generic
        err = ctypes.get_last_error()
        raise RuntimeError(f"CredReadW({target!r}) failed — Win32 error {err}")

    try:
        cred = ptr.contents
        info: dict[str, Any] = {
            "target": cred.TargetName,
            "flags": cred.Flags,
            "type": cred.Type,
            "persist": cred.Persist,
            "comment": cred.Comment or "",
            "blob": b"",
            "attributes": [],
        }
        if cred.CredentialBlobSize and cred.CredentialBlob:
            info["blob"] = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)

        if cred.AttributeCount and cred.Attribute:
            for i in range(cred.AttributeCount):
                a = cred.Attribute[i]
                raw = b""
                if a.ValueLength and a.Value:
                    raw = ctypes.string_at(a.Value, a.ValueLength)
                info["attributes"].append((a.Name or f"attr{i}", a.ValueType, raw))

        return info
    finally:
        adv.CredFree(ptr)


# ── Secret parsing ────────────────────────────────────────────────────────────

def _mask(value: str) -> str:
    if not value:
        return "<missing>"
    if len(value) <= 12:
        return value[:2] + "…" + f"({len(value)} chars)"
    return f"{value[:6]}…{value[-4:]} ({len(value)} chars)"


def _expiry_from_value(value: Any) -> int | None:
    """Return epoch ms if the value looks like an absolute expiry, else None."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        v = float(value)
        if v >= 1e11:
            return int(v * 1000) if v < 1e14 else int(v)
        return None
    if isinstance(value, str):
        s = value.strip()
        # Go time.Time string: "2026-08-31 17:26:46.6172655 +0200 CEST"
        m = re.match(
            r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?)"
            r"(?:\s*([+-]\d{2}:?\d{2})|Z)?",
            s,
        )
        if m:
            base = m.group(1) + "T" + m.group(2)
            offset = m.group(3)
            try:
                if offset and ":" not in offset:
                    offset = offset[:3] + ":" + offset[3:]
                dt = datetime.fromisoformat(base + (offset or ""))
                if offset is None and not base.endswith(("Z",)) and dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return int(dt.timestamp() * 1000)
            except ValueError:
                return None
        try:
            return int(float(s))
        except ValueError:
            return None
    return None


def _find_expiry(obj: dict[str, Any]) -> int | None:
    for key in ("expiry_date", "expiry", "expires_at", "expiration_time",
                "expiration", "token_expiration", "expires"):
        if key in obj:
            got = _expiry_from_value(obj[key])
            if got is not None:
                return got
    for key, val in obj.items():
        if "expir" in key.lower():
            got = _expiry_from_value(val)
            if got is not None:
                return got
    return None


def _extract(obj: dict[str, Any], *names: str) -> str | None:
    for n in names:
        v = obj.get(n)
        if isinstance(v, str) and v:
            return v
    return None


def try_parse_secret(blob: bytes) -> dict[str, Any] | None:
    """Return token fields if the blob parses as a token, else None."""
    text: str | None = None
    try:
        text = blob.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = blob.decode("utf-16-le")
        except UnicodeDecodeError:
            import base64
            try:
                text = base64.b64decode(blob).decode("utf-8")
            except Exception:
                return None
    if not text or not text.strip():
        return None

    data: Any = None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None

    if isinstance(data, dict):
        inner = data.get("token")
        if isinstance(inner, dict):
            data = {**data, **inner}
        parsed = {
            "access_token": _extract(data, "access_token", "accessToken"),
            "refresh_token": _extract(data, "refresh_token", "refreshToken"),
            "expiry_date": _find_expiry(data),
            "client_id": _extract(data, "client_id", "clientId"),
            "client_secret": _extract(data, "client_secret", "clientSecret"),
        }
        if parsed["access_token"] or parsed["refresh_token"]:
            return parsed
        return None

    if not text.strip().startswith(("{", "[")):
        # bare-string secret: treat as an access token
        return {
            "access_token": text.strip(),
            "refresh_token": None,
            "expiry_date": None,
            "client_id": None,
            "client_secret": None,
        }
    return None


# ── Client pair extraction from agy.exe (fallback) ───────────────────────────

def extract_client_pairs() -> list[dict[str, str]]:
    if not AGY_EXE.exists():
        return []
    try:
        data = AGY_EXE.read_bytes()
    except OSError:
        return []
    ids = [m.decode() for m in CLIENT_ID_RE.findall(data)]
    secrets = [m.decode() for m in CLIENT_SECRET_RE.findall(data)]
    pairs: list[dict[str, str]] = []
    for cid, sec in zip(ids, secrets):
        pairs.append({"client_id": cid, "client_secret": sec})
    return pairs


# ── Main ──────────────────────────────────────────────────────────────────────

def _diagnose(info: dict[str, Any]) -> None:
    print(f"  target   : {info['target']!r}")
    print(f"  type     : {info['type']}  flags={info['flags']}  persist={info['persist']}")
    print(f"  blob     : {len(info['blob'])} bytes")
    if info["comment"]:
        print(f"  comment  : {info['comment'][:80]!r}")
    for name, vtype, raw in info["attributes"]:
        print(f"  attribute: {name!r} type={vtype} {len(raw)} bytes")


def _try_target(target: str) -> dict[str, Any] | None:
    """Read the credential under one name; return parsed token or None."""
    info = read_credential(target)
    _diagnose(info)

    # candidate secrets: blob first, then attributes (largest first),
    # then comment as last resort
    candidates: list[tuple[str, bytes]] = [("blob", info["blob"])]
    candidates += [
        (f"attribute:{name}", raw)
        for name, _vtype, raw in sorted(info["attributes"], key=lambda x: -len(x[2]))
    ]
    if info["comment"]:
        candidates.append(("comment", info["comment"].encode("utf-8")))

    for name, raw in candidates:
        if not raw:
            continue
        p = try_parse_secret(raw)
        if p is not None:
            print(f"  -> token found in {name}")
            return p
    return None


def main() -> int:
    parsed: dict[str, Any] | None = None
    for target in CRED_TARGETS:
        print(f"\nTrying credential {target!r} …")
        try:
            parsed = _try_target(target)
        except RuntimeError as exc:
            print(f"  {exc}")
        if parsed is not None:
            break

    if parsed is None:
        raise RuntimeError(
            "No parseable token found under any known credential name.\n"
            "  (If the diagnostics above show a non-empty blob/attribute that we\n"
            "   couldn't parse, the secret may be DPAPI-wrapped; report the output.)\n"
            "  Fallback: run 'gemini login' once — the gemini CLI writes the\n"
            "  same creds file directly, refresh token included."
        )

    pairs = extract_client_pairs()
    print(f"  client pairs from agy.exe: {len(pairs)}")

    if not parsed.get("client_id") and pairs:
        parsed["client_id"] = pairs[0]["client_id"]
        parsed["client_secret"] = pairs[0]["client_secret"]
    if not parsed.get("client_id") and not pairs:
        print("WARNING: no client_id/client_secret found anywhere — token refresh will fail.")

    creds: dict[str, Any] = {"token_uri": "https://oauth2.googleapis.com/token"}
    if parsed.get("refresh_token"):
        creds["refresh_token"] = parsed["refresh_token"]
    if parsed.get("access_token"):
        creds["access_token"] = parsed["access_token"]
    if parsed.get("expiry_date"):
        creds["expiry_date"] = parsed["expiry_date"]
    if pairs:
        creds["oauth_pairs"] = pairs
    if parsed.get("client_id") and parsed.get("client_secret"):
        creds["client_id"] = parsed["client_id"]
        creds["client_secret"] = parsed["client_secret"]

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(creds, indent=2) + "\n", encoding="utf-8")
    try:
        OUT_FILE.chmod(0o600)
    except OSError:
        pass

    print()
    print("Wrote:", OUT_FILE)
    print(f"  refresh_token : {_mask(parsed.get('refresh_token') or '')}")
    print(f"  access_token  : {_mask(parsed.get('access_token') or '')}")
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    exp = parsed.get("expiry_date")
    if exp:
        print(f"  expiry_date   : {exp} ({(exp - now_ms) / 60_000:+.0f} min from now)")
    else:
        print("  expiry_date   : <not in blob — provider will refresh immediately>")
    for p in pairs:
        print(f"    pair: {p['client_id']} / {_mask(p['client_secret'])}")

    if not parsed.get("refresh_token"):
        print()
        print("!! WARNING: no refresh_token found. The access token works until it")
        print("!! expires, then refresh will fail. Fallback: run 'gemini login' once")
        print("!! (the gemini CLI writes the same file WITH a refresh token).")
    print()
    print("Done. The gateway picks this up on the next snapshot poll (no restart).")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
