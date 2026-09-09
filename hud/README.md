# coding-limits HUD (Windows)

A small, always-on-top, semi-transparent Windows tray app that mirrors the
coding-limits dashboard in the corner of your screen, so the rate-limit windows
for Codex / Claude / Gemini are always visible.

```
┌────────────────────────────────────────────┐
│  AI Limits                        updated 4s ago   │
│  ● Codex · pro plan                 [LIVE]        │
│      (5h ring 0%)      (7d ring 68%)              │
│      resets in 2h 4m   resets in 3d 1h            │
│  ● Claude · pro plan                [LIVE]        │
│      (5h ring 43%)     (7d ring 83%)              │
│  ● Gemini · personal              [LIVE]          │
│      (RPM 100%)        (RPD 100%)                 │
└────────────────────────────────────────────┘
```

The app also **runs the gateway itself**: on startup it launches
`python server.py` from the coding-limits repo (as a child process), keeps it
alive, and retires the old `tray.py` / `start-gateway.ps1` setup (now in
`legacy/`).

## Requirements

- Windows 10/11
- [.NET 9 Desktop Runtime](https://dotnet.microsoft.com/download/dotnet/9.0) (or the .NET 9 SDK to build)
- Python 3.13 (or any 3.9+; the default install `C:\Python313\python.exe` is
  tried first, then `python` / `python3` on `PATH`)
- The coding-limits repo (the HUD locates it relative to its own folder)

## Build

```powershell
cd hud
dotnet build -c Release
# or, matching the shipped layout:
dotnet publish -c Release -r win-x64 --self-contained false -o bin\Release\net9.0-windows
```

The app is framework-dependent, so the target machine needs the .NET 9
Desktop Runtime.

## Run

```powershell
.\bin\Release\net9.0-windows\CodingLimitsHud.exe
```

- The HUD appears in the **top-left** of the screen by default (drag it to
  move; the position is remembered).
- It is **always on top**, **borderless**, and **semi-transparent**.
- The **gateway starts automatically** as a child process (see
  [Gateway management](#gateway-management)) and the app **starts at login**
  (see [Login autostart](#login-autostart)).
- A **tray icon** appears: left-click / double-click to show or hide,
  right-click for a menu:
  - **Show HUD** / **Hide HUD**
  - **Refresh now**
  - **Open dashboard**
  - **View logs** (opens `logs/gateway.log`)
  - **Start gateway** / **Stop gateway**
  - **Start at login** (checked by default; toggles the autostart registry key)
  - **Exit (keep gateway)** / **Exit & stop gateway**
- Only one instance runs at a time (enforced with a named mutex).

## Gateway management

The app owns the gateway lifecycle:

- On startup it starts `python server.py` (working dir = repo root) if the
  port isn't already serving — if a gateway is already running, it **adopts**
  it instead of starting a second one.
- If the gateway dies, the app restarts it automatically (once per second the
  port is down).
- The gateway's stdout/stderr is appended to `logs/gateway.log`.
- **Stop gateway** in the tray menu stops the child *and* tells the app not to
  restart it, until you pick **Start gateway** again.
- **Exit & stop gateway** quits and kills the gateway; **Exit (keep gateway)**
  quits and leaves it running (e.g. if you started it yourself).

### CLI flags (same exe)

```powershell
CodingLimitsHud.exe --start-gateway   # start/adopt, wait for the port, exit
CodingLimitsHud.exe --stop-gateway    # stop the gateway (HUD will restart it)
CodingLimitsHud.exe --autostart-on    # enable the login autostart key
CodingLimitsHud.exe --autostart-off   # disable it
CodingLimitsHud.exe --help
```

## Login autostart

By default the app registers itself under
`HKCU:\Software\Microsoft\Windows\CurrentVersion\Run` (value
`CodingLimitsHud`) so it — and therefore the gateway — are running as soon as
you log in. It self-heals the key on every launch, so simply running the exe
once is enough. Toggle it anytime via the tray menu (**Start at login**) or
the `--autostart-on` / `--autostart-off` flags.

## Configuration

Settings are read from `hud.config.json` **next to the executable**, with
environment variables taking precedence.

| File key / Env var | Default | Description |
|---|---|---|
| `url` / `CL_HUD_URL` | `http://127.0.0.1:8765` | Gateway base URL |
| `token` / `CL_HUD_TOKEN` | _(empty)_ | Value for the `X-Gauge-Token` header (if the gateway is secured) |
| `refreshSeconds` / `CL_HUD_REFRESH_SECONDS` | `30` | How often to re-fetch the snapshot (min 5) |
| `opacity` | `0.85` | Window transparency (0.2–1.0) |
| `x`, `y` | `8`, `8` | Initial window position (updated when you drag) |
| `startVisible` | `true` | Show the HUD on launch (otherwise start hidden in tray) |
| `autoStartGateway` | `true` | Start/keep the gateway running |
| `autostart` | `true` | (Re)register the login autostart key on launch |
| `pythonPath` / `CL_GATEWAY_PYTHON` | `C:\Python313\python.exe`, then PATH | Python executable for the gateway |
| `repoRoot` | _(auto: nearest ancestor dir with `server.py`)_ | coding-limits repo root |

Example `hud.config.json`:

```json
{
  "url": "http://192.168.1.10:8765",
  "token": "your-gauge-token",
  "refreshSeconds": 20,
  "opacity": 0.8,
  "x": 8,
  "y": 8,
  "startVisible": true,
  "autoStartGateway": true,
  "autostart": true,
  "pythonPath": "C:\\Python313\\python.exe"
}
```

## Notes

- The HUD polls `GET /api/v1/snapshot` and sends `X-Gauge-Token` when a token
  is configured (matching the gateway's auth).
- If the gateway is unreachable, the footer shows `gateway unreachable` and the
  tray tooltip is suffixed with `— gateway offline`; it keeps retrying (and
  restarts the gateway if it owns one).
- Gateway output lands in `logs/gateway.log` (the old launcher's log was
  rotated to `gateway.log.old-20260909`).
- Ring colours follow the dashboard: green ≥ 50%, amber ≥ 20%, red below.
- `resetsAt` values are rendered as a live countdown (e.g. `resets in 2h 4m`).

## Publish a single-file build (optional)

```powershell
dotnet publish -c Release -r win-x64 --self-contained false \
  -p:PublishSingleFile=true -o dist
```
