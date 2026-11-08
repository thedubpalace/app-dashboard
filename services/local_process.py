"""Host processes: launch hidden dev servers, find and kill them by port or name."""
from __future__ import annotations

import shlex
import socket
import subprocess
import sys
from pathlib import Path

import psutil

from services.process_window import NEW_GROUP, NO_WINDOW, hidden_startupinfo


def spawn_hidden(run_cmd: str, cwd: str | None) -> str | None:
    """Launch a command detached and windowless. Returns an error, or None."""
    if cwd and not Path(cwd).is_dir():
        cwd = None

    try:
        args = shlex.split(run_cmd, posix=False)
    except ValueError:
        args = run_cmd.split()

    try:
        subprocess.Popen(
            args,
            cwd=cwd,
            startupinfo=hidden_startupinfo(), creationflags=NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        # Fall back to shell so PATH-resolved commands (e.g. uvicorn) work.
        try:
            subprocess.Popen(run_cmd, cwd=cwd, shell=True, startupinfo=hidden_startupinfo(), creationflags=NO_WINDOW)
        except Exception as exc:  # noqa: BLE001
            return str(exc)
    except Exception as exc:  # noqa: BLE001
        return str(exc)
    return None


def spawn_restart(port: int | None, token: str | None, run_cmd: str, cwd: str | None) -> None:
    """Spawn a detached Python process that does stop → wait → start.

    Detached so it survives even when this server process is the one being restarted.
    """
    subprocess.Popen(
        [sys.executable, "-c", build_restart_script(port, token, run_cmd, cwd)],
        startupinfo=hidden_startupinfo(), creationflags=NO_WINDOW | NEW_GROUP,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )


def build_restart_script(port: int | None, token: str | None, run_cmd: str, cwd: str | None) -> str:
    return f"""
import time, subprocess, socket, shlex
startupinfo = None
if hasattr(subprocess, 'STARTUPINFO'):
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
from pathlib import Path
import psutil

time.sleep(0.7)

port = {port!r}
token = {token!r}
run_cmd = {run_cmd!r}
cwd = {cwd!r}

if port:
    for conn in psutil.net_connections(kind='inet'):
        if conn.laddr and conn.laddr.port == port and conn.pid:
            try: psutil.Process(conn.pid).terminate()
            except: pass
else:
    needle = (token or '').lower()
    for proc in psutil.process_iter(['name', 'cwd']):
        try:
            pname = (proc.info.get('name') or '').lower()
            if pname.endswith('.exe'): pname = pname[:-4]
            if pname == needle and cwd and proc.info.get('cwd') and Path(proc.info['cwd']).resolve() == Path(cwd).resolve(): proc.terminate()
        except: pass

if port:
    for _ in range(30):
        time.sleep(0.25)
        s = socket.socket()
        s.settimeout(0.5)
        try:
            if s.connect_ex(('localhost', port)) != 0:
                break
        finally:
            s.close()
else:
    time.sleep(1.0)

try:
    args = shlex.split(run_cmd, posix=False)
except Exception:
    args = run_cmd.split()
try:
    subprocess.Popen(args, cwd=cwd, startupinfo=startupinfo, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
except FileNotFoundError:
    subprocess.Popen(run_cmd, cwd=cwd, shell=True, startupinfo=startupinfo, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
"""


# Ports published by a container are held by Docker's proxy processes, not by
# the app. Killing one of those takes Docker Desktop down with every container
# on it — always stop the container instead.
_DOCKER_PROXY_PROCESSES = frozenset(
    {
        "com.docker.backend",
        "com.docker.proxy",
        "docker",
        "dockerd",
        "docker desktop",
        "vpnkit",
        "wslrelay",
        "wslhost",
        "wsl",
    }
)


def _is_docker_proxy(proc: psutil.Process) -> bool:
    try:
        name = proc.name().lower()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    if name.endswith(".exe"):
        name = name[:-4]
    return name in _DOCKER_PROXY_PROCESSES


def kill_by_port(port: int) -> int:
    killed = 0
    for conn in psutil.net_connections(kind="inet"):
        if conn.laddr and conn.laddr.port == port and conn.pid:
            try:
                proc = psutil.Process(conn.pid)
                if _is_docker_proxy(proc):
                    continue
                proc.terminate()
                killed += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    return killed


def kill_by_name(name: str, cwd: str | None = None) -> int:
    needle = name.lower()
    killed = 0
    for proc in psutil.process_iter(["name", "pid", "cwd"]):
        try:
            pname = (proc.info.get("name") or "").lower()
            if pname.endswith(".exe"):
                pname = pname[:-4]
            if pname == needle and (cwd is None or (proc.info.get("cwd") and Path(proc.info["cwd"]).resolve() == Path(cwd).resolve())):
                proc.terminate()
                killed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return killed


def is_port_open(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(1)
    try:
        return sock.connect_ex(("localhost", port)) == 0
    finally:
        sock.close()
