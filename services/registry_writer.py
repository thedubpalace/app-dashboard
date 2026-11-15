"""Targeted writer for `registry.json` (REQ-012).

Only `requirementsReady` and `requirementsApproval` of one project may be
changed. The file is never re-serialised: the writer locates the exact
character span of each value and splices in the new value, so every other
byte (key order, spacing, Thai text, other projects, `controlResources`)
stays as it was. The result is re-parsed and compared with the original
plus the intended change before it is written atomically under a lock.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from services.file_lock import file_lock, write_text_atomic

ALLOWED_FIELDS = ("requirementsReady", "requirementsApproval")
_WS = " \t\r\n"
_NUMBER = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?")


class RegistryWriteError(RuntimeError):
    pass


@dataclass
class Member:
    key: str
    key_start: int
    value_start: int
    value_end: int


@dataclass
class ObjectSpan:
    start: int
    end: int  # index just past the closing brace
    members: list[Member]


class _Scanner:
    """Minimal JSON scanner that records value spans (input must be valid JSON)."""

    def __init__(self, text: str):
        self.text = text

    def ws(self, i: int) -> int:
        while i < len(self.text) and self.text[i] in _WS:
            i += 1
        return i

    def string_end(self, i: int) -> int:
        assert self.text[i] == '"'
        i += 1
        while True:
            ch = self.text[i]
            if ch == "\\":
                i += 2
            elif ch == '"':
                return i + 1
            else:
                i += 1

    def value_end(self, i: int) -> int:
        ch = self.text[i]
        if ch == '"':
            return self.string_end(i)
        if ch == "{":
            return self.object(i).end
        if ch == "[":
            return self.array(i)[1]
        for literal in ("true", "false", "null"):
            if self.text.startswith(literal, i):
                return i + len(literal)
        match = _NUMBER.match(self.text, i)
        if not match:
            raise RegistryWriteError(f"unexpected character at {i}")
        return match.end()

    def object(self, i: int) -> ObjectSpan:
        start = i
        i = self.ws(i + 1)
        members: list[Member] = []
        if self.text[i] == "}":
            return ObjectSpan(start, i + 1, members)
        while True:
            key_start = i
            key_end = self.string_end(i)
            key = json.loads(self.text[key_start:key_end])
            i = self.ws(key_end)
            assert self.text[i] == ":"
            value_start = self.ws(i + 1)
            value_end = self.value_end(value_start)
            members.append(Member(key, key_start, value_start, value_end))
            i = self.ws(value_end)
            if self.text[i] == ",":
                i = self.ws(i + 1)
                continue
            assert self.text[i] == "}"
            return ObjectSpan(start, i + 1, members)

    def array(self, i: int) -> tuple[list[int], int]:
        starts: list[int] = []
        i = self.ws(i + 1)
        if self.text[i] == "]":
            return starts, i + 1
        while True:
            starts.append(i)
            i = self.ws(self.value_end(i))
            if self.text[i] == ",":
                i = self.ws(i + 1)
                continue
            assert self.text[i] == "]"
            return starts, i + 1


def _line_indent(text: str, index: int) -> str:
    line_start = text.rfind("\n", 0, index) + 1
    match = re.match(r"[ \t]*", text[line_start:index])
    return match.group(0) if match else ""


def _render(value: Any, indent: str, newline: str) -> str:
    if isinstance(value, (dict, list)) and value:
        lines = json.dumps(value, ensure_ascii=False, indent=2).split("\n")
        return newline.join([lines[0]] + [indent + line for line in lines[1:]])
    return json.dumps(value, ensure_ascii=False)


def plan_update(text: str, project: str, updates: dict[str, Any]) -> str:
    """Return the new registry text, or the same text when nothing changes."""
    unknown = set(updates) - set(ALLOWED_FIELDS)
    if unknown:
        raise RegistryWriteError(f"field not allowed: {sorted(unknown)}")
    original = json.loads(text)
    projects = original.get("projects") if isinstance(original, dict) else None
    if not isinstance(projects, list):
        raise RegistryWriteError("registry has no projects array")
    scanner = _Scanner(text)
    root = scanner.object(scanner.ws(0))
    projects_member = next((m for m in root.members if m.key == "projects"), None)
    if projects_member is None or text[projects_member.value_start] != "[":
        raise RegistryWriteError("registry has no projects array")
    starts, _ = scanner.array(projects_member.value_start)
    matches = [index for index, item in enumerate(projects) if isinstance(item, dict) and item.get("name") == project]
    if len(matches) != 1:
        raise RegistryWriteError(f"project {project!r} found {len(matches)} times")
    index = matches[0]
    current = projects[index]
    if all(key in current and current[key] == value for key, value in updates.items()):
        return text
    span = scanner.object(starts[index])
    newline = "\r\n" if "\r\n" in text else "\n"
    edits: list[tuple[int, int, str]] = []
    missing: list[str] = []
    for key, value in updates.items():
        member = next((m for m in span.members if m.key == key), None)
        if member is None:
            missing.append(key)
            continue
        if current.get(key) == value:
            continue
        edits.append((member.value_start, member.value_end, _render(value, _line_indent(text, member.key_start), newline)))
    if missing:
        if not span.members:
            raise RegistryWriteError("cannot insert into an empty project object")
        last = span.members[-1]
        indent = _line_indent(text, last.key_start)
        anchor_member = next((m for m in span.members if m.key == "requirementsReady"), None)
        anchor = anchor_member or last
        insert = "".join(f",{newline}{indent}{json.dumps(key)}: {_render(updates[key], indent, newline)}" for key in missing)
        edits.append((anchor.value_end, anchor.value_end, insert))
    new_text = text
    for start, end, replacement in sorted(edits, key=lambda e: e[0], reverse=True):
        new_text = new_text[:start] + replacement + new_text[end:]
    expected = json.loads(text)
    expected["projects"][index].update(updates)
    if json.loads(new_text) != expected:
        raise RegistryWriteError("targeted edit verification failed; registry not written")
    return new_text


def update_project_fields(path: Path, project: str, updates: dict[str, Any]) -> bool:
    """Apply a targeted update under a lock. Returns True when the file changed."""
    with file_lock(path):
        raw = path.read_bytes()
        bom = raw.startswith(b"\xef\xbb\xbf")
        text = raw[3:].decode("utf-8") if bom else raw.decode("utf-8")
        new_text = plan_update(text, project, updates)
        if new_text == text:
            return False
        write_text_atomic(path, ("﻿" if bom else "") + new_text)
        return True


def read_project(path: Path, project: str) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    for item in data.get("projects", []) if isinstance(data, dict) else []:
        if isinstance(item, dict) and item.get("name") == project:
            return item
    return None
