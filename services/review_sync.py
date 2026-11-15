"""One-way sync `.veda/projects/<name>/requirements.md` -> GitHub issues + project cards (REQ-004).

Ownership: requirement content belongs to `.veda/`; decisions (Approved /
Changes requested) belong to the tracker and are only written by the review
page. The sync may only write `Draft` and `Needs review` (REQ-003 b): any
other status raises `SyncPermissionError` before anything is sent.

Per REQ block:
- no card yet           -> create issue `REQ-NNN: <title>`, add it to the project,
                           set REQ-ID / App, Status = Needs review
- content changed       -> overwrite issue title/body; Approved / Changes requested
                           -> Draft -> Needs review; Draft -> Needs review
- issue edited on GitHub (local content unchanged) -> overwrite + drift log entry
- unchanged             -> nothing (idempotent); a card in Draft or without a
                           status is moved to Needs review (the sync is ba's
                           "request review" step)
- removed from requirements.md -> marked inactive in the index (id never reused)

All GitHub calls go through `services.github_projects.GitHubProjects`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services import requirements_index
from services.github_projects import (
    FIELD_REQ_ID, SYNC_STATUSES, GitHubProjects, ProjectItem, body_hash, issue_title, render_issue_body,
)
from services.review_parser import ParsedRequirement, parse_file
from services.review_tracker import APPROVED, CHANGES_REQUESTED, DRAFT, NEEDS_REVIEW, STATUSES


class SyncPermissionError(PermissionError):
    """The sync tried to write a reviewer-only status."""


class SyncAbort(RuntimeError):
    pass


@dataclass
class SyncAction:
    req_id: str
    action: str  # create | update | drift | status | fields | noop | withdraw
    detail: str = ""
    status_before: str | None = None
    status_after: str | None = None


@dataclass
class SyncReport:
    project: str
    dry_run: bool
    actions: list[SyncAction] = field(default_factory=list)

    @property
    def writes(self) -> int:
        return sum(action.action != "noop" for action in self.actions)

    def to_dict(self) -> dict[str, Any]:
        return {"project": self.project, "dryRun": self.dry_run, "writes": self.writes,
                "actions": [action.__dict__ for action in self.actions]}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def guard_status(status: str) -> str:
    if status not in SYNC_STATUSES:
        raise SyncPermissionError(f"sync may not set Status={status!r}; only {', '.join(SYNC_STATUSES)} (REQ-003)")
    return status


def local_requirements(projects_dir: Path, project: str) -> list[ParsedRequirement]:
    if not requirements_index.PROJECT_NAME.match(project):
        raise SyncAbort("invalid project name")
    path = projects_dir / project / "requirements.md"
    if not path.is_file():
        raise SyncAbort(f"{path} not found")
    items = parse_file(path, project)
    seen: set[str] = set()
    duplicates = sorted({item.req_id for item in items if item.req_id in seen or seen.add(item.req_id)})
    if duplicates:
        raise SyncAbort(f"duplicate REQ ids in requirements.md: {', '.join(duplicates)}")
    return items


def update_index_offline(projects_dir: Path, index_path: Path, project: str) -> list[str]:
    """Record local REQ blocks of a project in the index (no network). Returns the ids written."""
    items = local_requirements(projects_dir, project)
    data = requirements_index.load(index_path)
    conflicts = requirements_index.owner_conflicts(data, project, [item.req_id for item in items])
    if conflicts:
        raise SyncAbort(f"REQ ids owned by another project: {', '.join(conflicts)}")
    entries = {}
    for item in items:
        current = data["requirements"].get(item.req_id) or {}
        wanted = {"project": project, "title": item.title, "contentHash": item.content_hash, "active": True}
        if any(current.get(key) != value for key, value in wanted.items()):
            entries[item.req_id] = wanted
    gone = [req_id for req_id, entry in data["requirements"].items()
            if entry.get("project") == project and entry.get("active") and req_id not in {item.req_id for item in items}]
    for req_id in gone:
        entries[req_id] = {"project": project, "active": False}
    if entries:
        requirements_index.record(index_path, entries)
    return sorted(entries)


class Syncer:
    def __init__(self, ops: GitHubProjects, *, projects_dir: Path, index_path: Path, log_path: Path | None):
        self.ops = ops
        self.projects_dir = projects_dir
        self.index_path = index_path
        self.log_path = log_path

    def _log(self, project: str, action: SyncAction, extra: dict[str, Any]) -> None:
        if not self.log_path:
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"at": _now(), "project": project, "reqId": action.req_id, "action": action.action, "detail": action.detail,
                 "statusBefore": action.status_before, "statusAfter": action.status_after, **extra}
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _set_status(self, item_id: str, status: str, dry_run: bool) -> None:
        guard_status(status)
        if not dry_run:
            self.ops.set_status_option(item_id, status)

    def run(self, project: str, *, dry_run: bool = False) -> SyncReport:
        report = SyncReport(project, dry_run)
        items = local_requirements(self.projects_dir, project)
        index = requirements_index.load(self.index_path)
        conflicts = requirements_index.owner_conflicts(index, project, [item.req_id for item in items])
        if conflicts:
            raise SyncAbort(f"REQ ids owned by another project: {', '.join(conflicts)}")
        problems = self.ops.meta().missing()
        if problems:
            raise SyncAbort(f"project is not set up ({', '.join(problems)}); run: review_sync.py bootstrap")
        cards: dict[str, ProjectItem] = {}
        for card in self.ops.items():
            if card.archived or card.repository.lower() != self.ops.repo.lower() or not card.req_id:
                continue
            if card.app != project:
                if any(item.req_id == card.req_id for item in items):
                    raise SyncAbort(f"{card.req_id} already has a card for project {card.app!r}")
                continue
            cards.setdefault(card.req_id, card)
        records: dict[str, dict[str, Any]] = {}
        for item in items:
            action = self._sync_one(project, item, cards.get(item.req_id), index["requirements"].get(item.req_id) or {}, records, dry_run)
            report.actions.append(action)
        local_ids = {item.req_id for item in items}
        for req_id, entry in index["requirements"].items():
            if entry.get("project") == project and entry.get("active") and req_id not in local_ids:
                action = SyncAction(req_id, "withdraw", "removed from requirements.md; id stays reserved")
                report.actions.append(action)
                records[req_id] = {"project": project, "active": False}
                if not dry_run:
                    self._log(project, action, {})
        if records and not dry_run:
            requirements_index.record(self.index_path, records)
        return report

    def _sync_one(self, project: str, item: ParsedRequirement, card: ProjectItem | None, entry: dict[str, Any],
                  records: dict[str, dict[str, Any]], dry_run: bool) -> SyncAction:
        title, body = issue_title(item), render_issue_body(item)
        base = {"project": project, "title": item.title, "contentHash": item.content_hash, "active": True}
        if card is None:
            action = SyncAction(item.req_id, "create", "new issue + card", None, NEEDS_REVIEW)
            if dry_run:
                return action
            issue_id, number = entry.get("issueNodeId"), entry.get("issueNumber")
            if not issue_id:
                issue = self.ops.create_issue(title, body)
                issue_id, number = issue["id"], issue["number"]
                # Record the issue at once so a crash before the card exists never creates a second issue.
                requirements_index.record(self.index_path, {item.req_id: {**base, "issueNodeId": issue_id, "issueNumber": number}})
            else:
                self.ops.update_issue(issue_id, title, body)
                action.detail = "existing issue re-attached as a card"
            item_id = self.ops.add_item(issue_id)
            self.ops.set_text(item_id, FIELD_REQ_ID, item.req_id)
            self.ops.set_text(item_id, self.ops.app_field, project)
            self._set_status(item_id, NEEDS_REVIEW, dry_run)
            records[item.req_id] = {**base, "issueNodeId": issue_id, "issueNumber": number, "itemId": item_id}
            self._log(project, action, {"issueNumber": number, "to": item.content_hash})
            return action

        synced = body_hash(card.issue_body)
        status = card.status if card.status in STATUSES else None
        content_changed = synced != item.content_hash
        body_differs = card.issue_body != body or card.issue_title != title
        if content_changed:
            kind, detail = "update", f"content changed {(synced or 'none')[:7]} -> {item.content_hash[:7]}"
        elif body_differs:
            kind, detail = "drift", "issue edited on GitHub; overwritten from requirements.md"
        else:
            kind, detail = "noop", ""
        path: list[str] = []
        if content_changed and status in (APPROVED, CHANGES_REQUESTED):
            path = [DRAFT, NEEDS_REVIEW]
        elif status in (None, DRAFT):
            path = [NEEDS_REVIEW]
        if kind == "noop" and path:
            kind, detail = "status", "request review"
        action = SyncAction(item.req_id, kind, detail, card.status, path[-1] if path else card.status)
        wanted = {**base, "issueNodeId": card.issue_id, "issueNumber": card.issue_number, "itemId": card.item_id}
        if any(entry.get(key) != value for key, value in wanted.items()):
            records[item.req_id] = wanted
        if kind == "noop" or dry_run:
            for status_name in path:
                guard_status(status_name)
            return action
        if body_differs:
            self.ops.update_issue(card.issue_id, title, body)
        for status_name in path:
            self._set_status(card.item_id, status_name, dry_run)
        self._log(project, action, {"issueNumber": card.issue_number, "from": synced, "to": item.content_hash})
        return action


def bootstrap(ops: GitHubProjects, *, dry_run: bool) -> dict[str, Any]:
    """Check the repo + project (REQ-001 a) and create the two text fields when missing."""
    result: dict[str, Any] = {"repository": ops.repo, "created": [], "manual": []}
    ops.repository_id()
    meta = ops.meta()
    result["project"] = {"title": meta.title, "url": meta.url}
    for name in (FIELD_REQ_ID, ops.app_field):
        if name not in meta.fields:
            if not dry_run:
                ops.create_text_field(name)
            result["created"].append(f"field '{name}' (text)" + (" [dry-run]" if dry_run else ""))
    meta = ops.meta(refresh=not dry_run)
    planned = {f"field '{FIELD_REQ_ID}'", f"field '{ops.app_field}'"} if dry_run else set()
    # Status options are edited in the GitHub UI (see README); they are reported, not created.
    result["manual"] = [problem for problem in meta.missing() if problem not in planned]
    result["ok"] = not result["manual"]
    return result
