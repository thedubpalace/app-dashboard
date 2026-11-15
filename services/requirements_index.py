"""`.veda/requirements-index.json` — REQ-id issuance and tracker mapping (REQ-002).

Shape::

    {
      "version": 1,
      "nextNumber": 19,
      "requirements": {
        "REQ-001": {"project": "app-dashboard", "title": "...", "contentHash": "<sha256>",
                    "active": true, "issueNumber": 12, "issueNodeId": "I_...",
                    "itemId": "PVTI_...", "createdAt": "...", "updatedAt": "..."}
      }
    }

Rules: an id is never reused or renumbered; once an id belongs to a
project it cannot move to another project; numbers are issued under a file
lock so two concurrent `allocate` calls never hand out the same number.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from services.file_lock import file_lock, write_text_atomic

REQ_ID = re.compile(r"^REQ-(\d{3,})$")
PROJECT_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class IndexError_(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def format_id(number: int) -> str:
    return f"REQ-{number:03d}"


def number_of(req_id: str) -> int:
    match = REQ_ID.match(req_id)
    if not match:
        raise IndexError_(f"invalid REQ id {req_id!r}")
    return int(match.group(1))


def empty() -> dict[str, Any]:
    return {"version": 1, "nextNumber": 1, "requirements": {}}


def load(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return empty()
    except (OSError, ValueError) as exc:
        raise IndexError_(f"cannot read {path.name}: {exc.__class__.__name__}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("requirements"), dict):
        raise IndexError_(f"{path.name} has an unexpected shape")
    data.setdefault("version", 1)
    data.setdefault("nextNumber", 1)
    return data


def _save(path: Path, data: dict[str, Any]) -> None:
    ordered = dict(sorted(data["requirements"].items(), key=lambda item: number_of(item[0])))
    data = {**data, "requirements": ordered}
    data["nextNumber"] = max([data.get("nextNumber", 1)] + [number_of(key) + 1 for key in ordered])
    write_text_atomic(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def allocate(path: Path, project: str, count: int = 1, *, reserved: Iterable[str] = ()) -> list[str]:
    """Issue `count` new ids for `project`. `reserved` = ids already written in any requirements.md."""
    if not PROJECT_NAME.match(project):
        raise IndexError_("invalid project name")
    if count < 1 or count > 100:
        raise IndexError_("count must be 1..100")
    with file_lock(path):
        data = load(path)
        floor = max([data["nextNumber"]] + [number_of(key) + 1 for key in data["requirements"]] + [number_of(r) + 1 for r in reserved])
        ids = [format_id(floor + offset) for offset in range(count)]
        now = _now()
        for req_id in ids:
            data["requirements"][req_id] = {"project": project, "title": None, "contentHash": None, "active": False,
                                            "reservedAt": now, "createdAt": now, "updatedAt": now}
        data["nextNumber"] = floor + count
        _save(path, data)
    return ids


def record(path: Path, entries: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Merge mapping fields for existing/new ids. Refuses to move an id to another project."""
    with file_lock(path):
        data = load(path)
        now = _now()
        for req_id, fields in entries.items():
            number_of(req_id)
            current = data["requirements"].get(req_id)
            project = fields.get("project")
            if current and project and current.get("project") != project:
                raise IndexError_(f"{req_id} already belongs to {current.get('project')}; ids are global and never reused")
            merged = {**(current or {"createdAt": now}), **{k: v for k, v in fields.items() if v is not None}}
            merged["updatedAt"] = now
            if merged.get("active"):
                merged.pop("reservedAt", None)
            data["requirements"][req_id] = merged
        _save(path, data)
        return data


def owner_conflicts(data: dict[str, Any], project: str, req_ids: Iterable[str]) -> list[str]:
    """Ids found in `project`'s requirements.md that the index assigns to another project."""
    conflicts = []
    for req_id in req_ids:
        entry = data["requirements"].get(req_id)
        if entry and entry.get("project") not in (None, project):
            conflicts.append(f"{req_id} (owned by {entry.get('project')})")
    return conflicts
