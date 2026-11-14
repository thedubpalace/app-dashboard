"""Append-only JSON-lines logs: the audit trail and the security log.

- Audit (``DASHBOARD_AUDIT_LOG``, default ``.runtime/audit.jsonl``): one
  ``requested`` record before every authorised state-changing request runs and
  one ``result`` record with its HTTP status afterwards. If the ``requested``
  record cannot be written the request is refused (fail closed).
- Security (``DASHBOARD_SECURITY_LOG``, default ``.runtime/security.log``):
  every 401/403 the access layer returns.

Records hold who/what/when only: never request bodies, cookies, tokens or
passwords.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
_lock = threading.Lock()


def audit_path() -> Path:
    return Path(os.getenv("DASHBOARD_AUDIT_LOG", str(BASE_DIR / ".runtime" / "audit.jsonl")))


def security_path() -> Path:
    return Path(os.getenv("DASHBOARD_SECURITY_LOG", str(BASE_DIR / ".runtime" / "security.log")))


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _append(path: Path, entry: dict[str, Any]) -> None:
    line = json.dumps({"at": _now(), **entry}, ensure_ascii=False) + "\n"
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)


def audit(entry: dict[str, Any]) -> None:
    """Raises OSError when the record cannot be written."""
    _append(audit_path(), entry)


def security(entry: dict[str, Any]) -> None:
    try:
        _append(security_path(), entry)
    except OSError:
        pass
