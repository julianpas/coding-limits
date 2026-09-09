#!/usr/bin/env python3
"""coding-limits tray app.

Sits in the Windows notification tray, keeps the gateway alive, and offers
quick actions (dashboard, logs, start/stop, exit). Single-instance: a second
launch just exits. Uses only pystray + Pillow on top of the stdlib.
"""
from __future__ import annotations

import msvcrt
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

REPO = Path(__file__).resolve().parent
LOG_DIR = REPO / "logs"
LOG_PATH = LOG_DIR / "gateway.log"
LOCK_PATH = LOG_DIR / "tray.lock"
URL = "http://localhost:8765"
PORT = 8765

state: dict = {"proc": None, "logfh": None}
_lock_handle = None  # kept open for the whole process lifetime (single instance)


def _debug(msg: str) -> None:
    try:
        LOG_DIR.mkdir(exist_ok=True)
        with open(LOG_DIR / "tray-debug.log", "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


def _acquire_single_instance() -> bool:
    global _lock_handle
    LOG_DIR.mkdir(exist_ok=True)
    fh = open(LOCK_PATH, "a+b")
    try:
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        return False  # another tray app instance owns the lock
    _lock_handle = fh
    return True


def port_up() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=0.5):
            return True
    except OSError:
        return False


def _find_gateway_pid() -> int | None:
    """PID of whatever process is LISTENING on the gateway port (fallback
    for gateways started outside this app)."""
    try:
        out = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True,
            text=True,
            timeout=5,
            # no console window flash from a console-less (pythonw) parent
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[3] == "LISTENING" and parts[1].endswith(f":{PORT}"):
                return int(parts[4])
    except Exception:
        pass
    return None


def start_gateway(icon: pystray.Icon | None = None) -> None:
    if port_up():
        return
    LOG_DIR.mkdir(exist_ok=True)
    logfh = open(LOG_PATH, "ab", buffering=0)
    logfh.write(f"\n--- gateway started by tray app {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n".encode())
    proc = subprocess.Popen(
        [sys.executable, "server.py"],
        cwd=str(REPO),
        stdout=logfh,
        stderr=subprocess.STDOUT,
        # NO_WINDOW: never pop a console window; NEW_PROCESS_GROUP: Ctrl+C-ish
        # signals from one console cannot reach the gateway.
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    state["proc"] = proc
    state["logfh"] = logfh
    if icon is not None:
        try:
            icon.notify("Gateway starting", f"{URL} coming up shortly")
        except Exception:
            pass


def stop_gateway(icon: pystray.Icon | None = None) -> None:
    proc = state["proc"]
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    else:
        # Gateway was started outside this app - find it by port and kill it.
        pid = _find_gateway_pid()
        if pid is not None:
            try:
                os.kill(pid, signal.SIGTERM)  # SIGTERM == TerminateProcess on Windows
            except OSError:
                pass
    if icon is not None:
        try:
            icon.notify("AI Limits", "Gateway stopped")
        except Exception:
            pass


def make_icon_image() -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([4, 4, 60, 60], radius=14, fill=(19, 26, 34, 255), outline=(48, 64, 84, 255), width=2)
    d.arc([16, 16, 48, 48], start=30, end=330, fill=(52, 211, 153, 255), width=6)
    d.line([32, 32, 43, 21], fill=(230, 237, 243, 255), width=3)
    d.ellipse([28, 28, 36, 36], fill=(230, 237, 243, 255))
    return img


def open_dashboard(icon, item) -> None:
    webbrowser.open(URL)


def view_logs(icon, item) -> None:
    if LOG_PATH.exists():
        os.startfile(str(LOG_PATH))


def menu_start(icon, item) -> None:
    start_gateway(icon)
    time.sleep(1.5)  # give the server a moment to bind
    try:
        refresh(icon)  # sync menu/tooltip right away
    except Exception:
        pass


def menu_stop(icon, item) -> None:
    stop_gateway(icon)
    time.sleep(0.5)
    try:
        refresh(icon)
    except Exception:
        pass


def exit_app(icon, item, stop=False) -> None:
    if stop:
        stop_gateway(icon)
    icon.stop()
    os._exit(0)


def build_menu(up: bool) -> pystray.Menu:
    return pystray.Menu(
        pystray.MenuItem("Open dashboard", open_dashboard),
        pystray.MenuItem("View logs", view_logs),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Start gateway", menu_start, enabled=not up),
        pystray.MenuItem("Stop gateway", menu_stop, enabled=up),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit (keep gateway running)", lambda i, it: exit_app(i, it, stop=False)),
        pystray.MenuItem("Exit & stop gateway", lambda i, it: exit_app(i, it, stop=True)),
    )


def refresh(icon: pystray.Icon) -> None:
    up = port_up()
    title = "AI Limits — running" if up else "AI Limits — stopped"
    if getattr(icon, "title", None) != title:
        icon.title = title
    # Only swap the native menu when the state actually changes - rebuilding
    # it every tick would DestroyMenu the menu under the user's cursor.
    if state.get("menu_up") != up:
        state["menu_up"] = up
        icon.menu = build_menu(up)


def status_loop(icon: pystray.Icon) -> None:
    while True:
        try:
            refresh(icon)
        except Exception as exc:
            _debug(f"status_loop: refresh failed: {exc!r}")
        time.sleep(5)


def main() -> None:
    if not _acquire_single_instance():
        return  # a tray app instance is already running
    icon = pystray.Icon("coding-limits", make_icon_image(), "AI Limits", build_menu(False))
    threading.Thread(target=status_loop, args=(icon,), daemon=True).start()
    # Auto-start the gateway if it isn't running yet (keeps the shortcut useful).
    if not port_up():
        threading.Thread(target=start_gateway, args=(icon,), daemon=True).start()
    icon.run_detached()
    while True:  # keep the main thread alive; the icon loop runs in its own thread
        time.sleep(1)


if __name__ == "__main__":
    main()
