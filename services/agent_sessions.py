"""Claude Code and Codex terminals opened from the dashboard: launch, count, kill."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import psutil

from services import app_registry
from services.settings_store import norm_path


def find_claude_executable() -> str | None:
    """Find the Claude Code CLI even when this server's PATH predates its install."""
    from_path = shutil.which("claude") or shutil.which("claude.exe")
    if from_path:
        return from_path

    # The native installer puts claude.exe in ~/.local/bin; npm installs use
    # %APPDATA%\npm. Neither is guaranteed to be on a service's PATH.
    candidates = [
        Path.home() / ".local" / "bin" / "claude.exe",
        Path.home() / "AppData" / "Roaming" / "npm" / "claude.cmd",
    ]
    for path in candidates:
        if path.is_file():
            return str(path)
    return None


def find_codex_executable() -> str | None:
    """Find the Codex CLI even when this server's PATH predates its install."""
    from_path = shutil.which("codex") or shutil.which("codex.exe")
    if from_path:
        return from_path

    # Codex Desktop installs versioned CLI binaries here on Windows. Searching
    # this narrow directory makes the launcher resilient to stale server PATHs.
    install_dir = Path.home() / "AppData" / "Local" / "OpenAI" / "Codex" / "bin"
    try:
        candidates = [path for path in install_dir.glob("*/codex.exe") if path.is_file()]
    except OSError:
        return None
    if not candidates:
        return None
    return str(max(candidates, key=lambda path: path.stat().st_mtime))


def _open_console(command: str) -> None:
    subprocess.Popen(
        ["powershell", "-NoExit", "-Command", command],
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )


def open_claude(cwd: str, claude_exe: str) -> None:
    safe_cwd = str(cwd).replace("'", "''")
    safe_claude = claude_exe.replace("'", "''")
    _open_console(f"Set-Location -LiteralPath '{safe_cwd}'; & '{safe_claude}' remote-control")


def open_codex(cwd: str, codex_exe: str) -> None:
    safe_cwd = str(cwd).replace("'", "''")
    safe_codex = codex_exe.replace("'", "''")
    _open_console(f"$env:HOME = $env:USERPROFILE; Set-Location -LiteralPath '{safe_cwd}'; & '{safe_codex}'")


def find_claude_sessions(
    app_path: str | None = None, processes: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Return one entry per PowerShell terminal running claude remote-control.

    Pass app_path to filter to sessions whose Set-Location matches that path.
    Pass processes (from app_registry.snapshot_processes()) to reuse an existing
    process scan instead of taking a fresh one.
    """
    norm_filter = norm_path(app_path) if app_path else None
    if processes is None:
        processes = app_registry.snapshot_processes()
    results = []
    for proc in processes:
        name = (proc["name"] or "").lower().replace(".exe", "")
        if name not in ("powershell", "pwsh"):
            continue
        cmdline = " ".join(str(c) for c in proc["cmdline"])
        if "remote-control" not in cmdline or "claude" not in cmdline.lower():
            continue
        m = re.search(r"Set-Location\s+(?:-LiteralPath\s+)?'([^']+)'", cmdline)
        path = m.group(1) if m else None
        if norm_filter and norm_path(path) != norm_filter:
            continue
        results.append({"pid": proc["pid"], "path": path})
    return results


def find_codex_sessions(
    app_path: str | None = None, processes: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Return dashboard-launched Codex terminals, optionally for one app path."""
    norm_filter = norm_path(app_path) if app_path else None
    if processes is None:
        processes = app_registry.snapshot_processes()
    results = []
    for proc in processes:
        name = (proc["name"] or "").lower().replace(".exe", "")
        if name not in ("powershell", "pwsh"):
            continue
        cmdline = " ".join(str(c) for c in proc["cmdline"])
        if "codex.exe" not in cmdline.lower():
            continue
        match = re.search(r"Set-Location\s+-LiteralPath\s+'([^']+)'", cmdline)
        path = match.group(1) if match else None
        if not path or (norm_filter and norm_path(path) != norm_filter):
            continue
        results.append({"pid": proc["pid"], "path": path})
    return results


def attach_session_counts(apps: list[dict[str, Any]], processes: list[dict[str, Any]]) -> None:
    for key, finder in (("claudeSessions", find_claude_sessions), ("codexSessions", find_codex_sessions)):
        counts: dict[str, int] = {}
        for session in finder(processes=processes):
            path = norm_path(session.get("path"))
            if path:
                counts[path] = counts.get(path, 0) + 1
        for app in apps:
            app[key] = counts.get(norm_path(app.get("localPath")), 0)


def kill_claude_sessions(app_path: str | None) -> int:
    killed = 0
    for s in find_claude_sessions(app_path=app_path):
        try:
            psutil.Process(s["pid"]).terminate()
            killed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return killed


def kill_codex_sessions(app_path: str | None) -> int:
    killed = 0
    for session in find_codex_sessions(app_path=app_path):
        try:
            root = psutil.Process(session["pid"])
            processes = root.children(recursive=True) + [root]
            for process in processes:
                process.terminate()
            gone, alive = psutil.wait_procs(processes, timeout=2)
            for process in alive:
                process.kill()
            killed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return killed
