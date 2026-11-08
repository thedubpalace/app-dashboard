"""Window startup settings for non-interactive Windows subprocesses."""
import subprocess


def hidden_startupinfo():
    """Hide the initial window as well as suppressing console allocation."""
    if not hasattr(subprocess, "STARTUPINFO"):
        return None
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = subprocess.SW_HIDE
    return info
