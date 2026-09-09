# coding-limits

A lightweight HTTP gateway that polls AI subscription usage APIs and exposes a single `/api/v1/snapshot` endpoint. Designed to be consumed by IoT devices, dashboards, or any client that needs live AI rate-limit data without direct API credentials on the client.

```json
{
  "ok": true,
  "fetchedAt": "2025-06-02T12:00:00Z",
  "providers": {
    "codex": { "shortWindow": { "remainingPercent": 72 }, "longWindow": { "remainingPercent": 55 } },
    "claude": { "shortWindow": { "remainingPercent": 12 }, "longWindow": { "remainingPercent": 44 } },
    "gemini": { "ok": true, "source": "gemini-api" }
  }
}
```

**Why a gateway?** Codex auth lives in the local CLI (`~/.codex`), Claude is accessed via a browser session cookie, and Gemini needs an API key — none of these belong on a microcontroller or a shared dashboard. The gateway runs on any always-on machine on your network and exposes a single authenticated endpoint that any client can poll.

---

## Supported providers

| Provider | Auth | Data |
|---|---|---|
| **OpenAI Codex** | Local CLI (`~/.codex`) | 5h and 7d rate limit windows with % remaining |
| **Claude** | Browser session key (`sessionKey` cookie) | 5h and 7d rate limit windows, overage credits |
| **Gemini** | OAuth credentials from `~/.gemini/oauth_creds.json` — no API key | Reachability + rate limit details on 429; auto-refreshes the access token |

---

## Quick start

```bash
git clone https://github.com/mortenlein/coding-limits
cd coding-limits

cp config.example.json config.json
python3 server.py
```

The gateway starts on `http://0.0.0.0:8765`. Test it:

```bash
curl http://localhost:8765/health
curl http://localhost:8765/api/v1/snapshot | python3 -m json.tool
```

---

## Production deployment

### Recommended: systemd on the host

```bash
# 1. Log in to Codex on the gateway machine (if using Codex provider)
codex login --device-auth

# 2. Deploy
sudo mkdir -p /opt/coding-limits
sudo cp -r . /opt/coding-limits/

sudo cp coding-limits.env.example /etc/coding-limits.env
sudo cp coding-limits@.service.example /etc/systemd/system/coding-limits@.service

sudo systemctl daemon-reload
sudo systemctl enable --now coding-limits@$USER
sudo systemctl status coding-limits@$USER
```

Edit `/etc/coding-limits.env` to configure providers and secrets.

Check logs:
```bash
sudo journalctl -u coding-limits@$USER -f
```

### Docker

```bash
docker compose -f docker-compose.example.yml up -d
```

---

## Configuration

Configuration is read from environment variables (via `EnvironmentFile` in the systemd unit) with optional override from `config.json`. **Environment variables take precedence.**

| Variable | Default | Description |
|---|---|---|
| `LISTEN_HOST` | `0.0.0.0` | Interface to bind |
| `LISTEN_PORT` | `8765` | Port to listen on |
| `DEVICE_NAME` | `AI Limits` | Name returned in snapshot metadata |
| `ACCESS_TOKEN` | _(empty)_ | Optional shared secret; clients send in `X-Gauge-Token` header |
| `CODEX_ENABLED` | `true` | Enable the Codex provider |
| `CODEX_PATH` | `codex` | Path to the `codex` binary |
| `CODEX_TIMEOUT_SECONDS` | `10` | Max wait for `codex app-server` |
| `CLAUDE_ENABLED` | `false` | Enable the Claude provider |
| `CLAUDE_SESSION_KEY` | _(empty)_ | `sessionKey` cookie from claude.ai |
| `CLAUDE_TIMEOUT_SECONDS` | `15` | Timeout for claude.ai requests |
| `GEMINI_ENABLED` | `false` | Enable the Gemini provider |
| `GEMINI_API_KEY` | _(empty)_ | Google AI Studio API key |
| `GEMINI_MODEL` | `gemini-2.0-flash` | Model to probe for reachability |
| `GEMINI_TIMEOUT_SECONDS` | `15` | Timeout for Gemini API requests |
| `PYTHONUNBUFFERED` | — | Set to `1` for `journalctl` output |

### Enabling Claude

1. Open [claude.ai](https://claude.ai) and log in
2. Open DevTools → Application → Cookies → `https://claude.ai`
3. Copy the value of the `sessionKey` cookie (starts with `sk-ant-sid-`)

```
CLAUDE_ENABLED=true
CLAUDE_SESSION_KEY=sk-ant-sid-...
```

**The key expires when you log out of claude.ai.**

### Enabling Gemini

The Gemini provider uses the same OAuth credentials as the Gemini CLI — **no API key, no billing**. It probes the Code Assist backend that the `gemini` CLI itself uses (`cloudcode-pa.googleapis.com/v1internal:countTokens`) as your Google account's personal free quota, and auto-refreshes the access token using the stored refresh token.

**One-time setup:**

1. On the gateway machine, log in with the Gemini CLI (`gemini`, with `security.auth.selectedType: "oauth-personal"` in `~/.gemini/settings.json`) and complete the browser sign-in. This creates `~/.gemini/oauth_creds.json`.
2. Enable the provider in `config.json` (or via env):

```json
{ "providers": { "gemini": { "enabled": true } } }
```

```
GEMINI_ENABLED=true
```

**Token refresh needs a client pair, and it is found in this order:**

1. `client_id`/`client_secret` inside the credentials file (or its `oauth_pairs` list),
2. `providers.gemini.client_id`/`client_secret` in `config.json`,
3. the gemini CLI's public installed-app pair, built into the provider (it is a public constant in `@google/gemini-cli-core`).

Because of the built-in fallback, re-logging in with the `gemini` CLI (which rewrites the credentials file without the pair) does **not** break refresh — the provider falls back to the CLI pair and writes it back into the file.

When Google eventually invalidates the refresh token (rare — typically only on password change or account revocation), re-login with the `gemini` CLI.

> **Note:** Google does not expose remaining quota in successful API responses, so `usedPercent` will be `null` when not rate-limited. The bars on the display will show `--`. If a 429 is hit, the relevant window (RPM or RPD) fills to 100% with a reset countdown.

### Securing the endpoint

```
ACCESS_TOKEN=your-random-secret
```

Clients must send this in the `X-Gauge-Token` header.

---

## API reference

### `GET /health`

No auth required. Returns current provider status from cache.

```json
{
  "ok": true,
  "time": "2025-06-02T12:00:00Z",
  "version": "0.3.0",
  "providers": {
    "codex":  { "enabled": true,  "ok": true,  "lastFetchAt": "...", "stale": false },
    "claude": { "enabled": true,  "ok": true,  "lastFetchAt": "...", "stale": false },
    "gemini": { "enabled": false, "ok": null,  "lastFetchAt": null,  "stale": null  }
  }
}
```

### `GET /api/v1/snapshot`

Requires `X-Gauge-Token` header if `ACCESS_TOKEN` is set. Calls all enabled providers and returns a normalised snapshot.

```json
{
  "ok": true,
  "fetchedAt": "2025-06-02T12:00:00Z",
  "version": "0.3.0",
  "deviceName": "AI Limits",
  "providers": {
    "codex": {
      "enabled": true,
      "ok": true,
      "source": "codex-app-server",
      "planType": "pro",
      "shortWindow": { "label": "5h", "usedPercent": 28.0, "remainingPercent": 72, "windowDurationMins": 300, "resetsAt": 1780408200 },
      "longWindow":  { "label": "7d", "usedPercent": 45.0, "remainingPercent": 55, "windowDurationMins": 10080, "resetsAt": 1780840800 },
      "error": null
    },
    "claude": {
      "enabled": true,
      "ok": true,
      "source": "claude-web",
      "shortWindow": { "label": "5h", "usedPercent": 88.0, "remainingPercent": 12, "windowDurationMins": 300, "resetsAt": 1780408200 },
      "longWindow":  { "label": "7d", "usedPercent": 56.0, "remainingPercent": 44, "windowDurationMins": 10080, "resetsAt": 1780840800 },
      "credits": { "enabled": true, "usedCreditsCents": 500, "monthlyLimitCents": 10000 },
      "error": null
    },
    "gemini": {
      "enabled": true,
      "ok": true,
      "source": "gemini-cli-logs",
      "planType": "personal",
      "shortWindow": { "label": "RPM", "usedPercent": 6.7, "remainingPercent": 93, "windowDurationMins": 1, "resetsAt": null },
      "longWindow":  { "label": "RPD", "usedPercent": 12.0, "remainingPercent": 88, "windowDurationMins": 1440, "resetsAt": null },
      "error": null
    }
  }
}
```

`resetsAt` is a Unix timestamp (seconds). `remainingPercent` is `100 - usedPercent`, clamped 0–100. `null` means data not available.

---

## Clients

- **hud/** (in this repo) — Windows tray app (WinForms, .NET 9): a semi-transparent, always-on-top HUD in the corner of your screen with per-provider usage rings. It also starts and supervises the gateway (replacing the retired `tray.py` launcher) and can auto-start at Windows login. See [hud/README.md](hud/README.md).
- **[esp32-coding-limits](https://github.com/mortenlein/esp32-coding-limits)** — ESP32-S3 firmware that displays live provider bars on a small TFT screen.

---

## Development

### Running tests

```bash
python3 -m unittest discover tests/ -v
```

### Adding a provider

1. Create `providers/yourprovider.py` with a class that has a `fetch() -> dict` method. The dict must include: `enabled`, `ok`, `source`, `shortWindow`, `longWindow`, and `error` keys.
2. Register it in `providers/__init__.py`.
3. Wire it up in `server.py` following the same pattern as the existing entries in `build_snapshot()`.
4. Add env vars to `coding-limits.env.example` and `config.example.json`.
5. Add tests in `tests/test_providers.py`.

---

## License

MIT — see [LICENSE](LICENSE).
