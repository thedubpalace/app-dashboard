"""Parse `REQ-NNN` blocks from a Veda `requirements.md` (REQ-002 / REQ-004).

Block format (open question 4 in requirements.md, defined here):

    **REQ-007 Title text**
    - bullet lines (content)
    - AC: (a) first criterion (b) second criterion

A block starts at a line that is exactly a bold `REQ-NNN Title` and ends at
the next REQ block or the next `#` heading. The content hash covers the
whole normalised block, so any change to the title, content or acceptance
criteria produces a new hash.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

REQ_START = re.compile(r"^\*\*(REQ-\d{3,})\s+(.+?)\*\*\s*$")
HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
AC_LINE = re.compile(r"^\s*[-*]\s+(?:\*\*)?AC(?:\*\*)?\s*[:：]\s*(.*)$", re.IGNORECASE)
SCOPE_LINE = re.compile(r"^\s*[-*]\s+(?:\*\*)?(In scope|Out of scope|อยู่ในขอบเขต|ไม่อยู่ในขอบเขต)(?:\*\*)?\s*[:：]\s*(.*)$", re.IGNORECASE)


@dataclass
class ParsedRequirement:
    req_id: str
    title: str
    project: str
    content_markdown: str
    acceptance_criteria: list[str]
    scope_in: list[str] = field(default_factory=list)
    scope_out: list[str] = field(default_factory=list)
    scope_source: str = "none"  # "requirement" | "feature" | "none"
    group: str | None = None
    content_hash: str = ""
    raw_block: str = ""


def content_hash(block: str) -> str:
    normalised = "\n".join(line.rstrip() for line in block.replace("\r\n", "\n").split("\n")).strip()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def split_criteria(text: str) -> list[str]:
    text = text.strip()
    if not text:
        return []
    parts = re.split(r"(?:^|\s)\(([a-z]|\d{1,2})\)\s*", text)
    if len(parts) <= 1:
        return [text]
    items = [parts[index].strip() for index in range(2, len(parts), 2)]
    lead = parts[0].strip()
    if lead:
        items.insert(0, lead)
    return [item for item in items if item]


def _feature_scope(lines: list[str], first_req_line: int) -> tuple[list[str], list[str]]:
    scope_start = None
    for index in range(first_req_line - 1, -1, -1):
        heading = HEADING.match(lines[index])
        if heading and len(heading.group(1)) == 2 and heading.group(2).strip().lower().startswith("scope"):
            scope_start = index
            break
    if scope_start is None:
        return [], []
    scope_in: list[str] = []
    scope_out: list[str] = []
    target: list[str] | None = None
    for line in lines[scope_start + 1:first_req_line]:
        heading = HEADING.match(line)
        if heading:
            if len(heading.group(1)) <= 2:
                break
            label = heading.group(2).lower()
            target = scope_out if ("นอก" in label or "out" in label) else scope_in
            continue
        bullet = re.match(r"^\s*[-*]\s+(.*)$", line)
        if bullet and target is not None:
            target.append(bullet.group(1).strip())
    return scope_in, scope_out


def parse_requirements(text: str, project: str) -> list[ParsedRequirement]:
    lines = text.replace("\r\n", "\n").split("\n")
    starts = [index for index, line in enumerate(lines) if REQ_START.match(line)]
    if not starts:
        return []
    feature_in, feature_out = _feature_scope(lines, starts[0])
    results: list[ParsedRequirement] = []
    group: str | None = None
    group_lines = {index: HEADING.match(line).group(2).strip() for index, line in enumerate(lines) if HEADING.match(line)}
    for position, start in enumerate(starts):
        end = len(lines)
        for index in range(start + 1, len(lines)):
            if REQ_START.match(lines[index]) or HEADING.match(lines[index]):
                end = index
                break
        for index in sorted(group_lines):
            if index < start:
                group = group_lines[index]
        match = REQ_START.match(lines[start])
        assert match
        block_lines = lines[start:end]
        body: list[str] = []
        criteria: list[str] = []
        scope_in: list[str] = []
        scope_out: list[str] = []
        for line in block_lines[1:]:
            ac = AC_LINE.match(line)
            if ac:
                criteria.extend(split_criteria(ac.group(1)))
                continue
            scope = SCOPE_LINE.match(line)
            if scope:
                label = scope.group(1).lower()
                (scope_out if label.startswith(("out", "ไม่")) else scope_in).append(scope.group(2).strip())
                continue
            body.append(line)
        if scope_in or scope_out:
            source = "requirement"
        elif feature_in or feature_out:
            scope_in, scope_out, source = list(feature_in), list(feature_out), "feature"
        else:
            source = "none"
        raw = "\n".join(block_lines)
        results.append(ParsedRequirement(
            req_id=match.group(1),
            title=match.group(2).strip(),
            project=project,
            content_markdown="\n".join(body).strip(),
            acceptance_criteria=criteria,
            scope_in=scope_in,
            scope_out=scope_out,
            scope_source=source,
            group=group,
            content_hash=content_hash(raw),
            raw_block=raw,
        ))
    return results


def parse_file(path: Path, project: str) -> list[ParsedRequirement]:
    return parse_requirements(path.read_text(encoding="utf-8"), project)
