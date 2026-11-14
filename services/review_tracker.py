"""Requirement tracker adapter boundary (REQ-017).

Review logic (queue, transitions, gate, audit) talks to `RequirementTracker`
only. REQ-id is the primary key everywhere; tracker-specific ids (issue
number, ProjectV2 item id) stay inside an adapter.

Implementations:
- `MockTracker`: reads real REQ blocks from `.veda/projects/<name>/requirements.md`
  but keeps status and comments in a sandbox JSON file under the dashboard's
  ignored `.runtime/` directory. It never touches registry.json.
- `GitHubProjectTracker`: skeleton for the ProjectV2 + central-repo adapter.
  Not connected yet (no repo, Project or token exists).

Select with env `REVIEW_TRACKER=mock|github` (default mock).
"""
from __future__ import annotations

import json
import os
import secrets
import threading
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from services.review_parser import ParsedRequirement, content_hash, parse_file

NEEDS_REVIEW = "Needs review"
APPROVED = "Approved"
CHANGES_REQUESTED = "Changes requested"
DRAFT = "Draft"
STATUSES = (DRAFT, NEEDS_REVIEW, APPROVED, CHANGES_REQUESTED)
REVIEWER_STATUSES = (APPROVED, CHANGES_REQUESTED)

# Error codes shared with the UI (design R14.4).
ERROR_CODES = (
    "github_unavailable", "rate_limited", "token_missing", "token_expired", "token_scope",
    "session_expired", "forbidden", "conflict_hash", "conflict_state", "network_unknown",
)


class TrackerError(Exception):
    """A sanitized tracker failure. `message` is safe to show to the reviewer."""

    def __init__(self, code: str, message: str, *, status: int = 503, retry_at: str | None = None, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retry_at = retry_at
        self.details = details or {}


@dataclass
class ReviewComment:
    id: str
    at: str
    actor: str
    kind: str  # "changes_requested" | "approved" | "note"
    body: str
    content_hash: str
    reason: str | None = None


@dataclass
class Card:
    req_id: str
    project: str
    title: str
    content_markdown: str
    acceptance_criteria: list[str]
    scope_in: list[str]
    scope_out: list[str]
    scope_source: str
    content_hash: str
    section_hashes: dict[str, str]
    status: str
    requested_at: str | None
    issue_number: int | None
    issue_url: str | None
    issue_is_mock: bool
    pr_url: str | None = None
    group: str | None = None
    comments: list[ReviewComment] = field(default_factory=list)
    approvals: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TrackerHealth:
    ok: bool = True
    token_expires_at: str | None = None
    warnings: list[dict[str, Any]] = field(default_factory=list)


class RequirementTracker(ABC):
    """Adapter contract. Adapters raise `TrackerError` with a code from ERROR_CODES."""

    mode: str = "abstract"

    @abstractmethod
    def health(self) -> TrackerHealth: ...

    @abstractmethod
    def list_cards(self, project: str | None = None) -> list[Card]:
        """All active cards (any status), optionally for one project."""

    def list_pending(self) -> list[Card]:
        cards = [card for card in self.list_cards() if card.status == NEEDS_REVIEW]
        return sorted(cards, key=lambda card: (card.requested_at or "", card.req_id))

    @abstractmethod
    def get(self, req_id: str) -> Card | None:
        """Re-fetch one card from the source of truth (never cached)."""

    @abstractmethod
    def set_status(self, req_id: str, status: str, *, actor: str, content_hash: str) -> None: ...

    @abstractmethod
    def add_comment(self, req_id: str, text: str, *, actor: str, kind: str, content_hash: str, reason: str | None = None) -> str:
        """Post a comment and return its id."""

    def projects(self) -> list[str]:
        return sorted({card.project for card in self.list_cards()})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def section_hashes(item: ParsedRequirement, suffix: str = "") -> dict[str, str]:
    return {
        "title": content_hash(item.title)[:12],
        "content": content_hash(item.content_markdown + suffix)[:12],
        "acceptanceCriteria": content_hash("\n".join(item.acceptance_criteria))[:12],
    }


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


# Faults the Admin debug toggle can simulate in mock mode (design R7).
SIMULATIONS = {
    "github_unavailable": "GitHub ไม่ตอบ (5xx/timeout)",
    "rate_limited": "GitHub rate limit",
    "token_missing": "ยังไม่ได้ตั้ง token",
    "token_expired": "token หมดอายุ/ถูกเพิกถอน",
    "token_scope": "token สิทธิ์ไม่พอ",
    "token_expiring": "token ใกล้หมดอายุ (5 วัน)",
    "comment_failed": "โพสต์คอมเมนต์ไม่สำเร็จ",
    "status_failed": "คอมเมนต์สำเร็จ แต่ตั้ง Status ไม่สำเร็จ (two-phase)",
    "network_unknown": "บันทึกแล้วแต่ตอบกลับไม่ถึง (ไม่ทราบผล)",
    "reconcile_failed": "บันทึกแล้วแต่ reconcile ไม่สำเร็จ",
}


class MockTracker(RequirementTracker):
    """Real requirement content, sandboxed decisions."""

    mode = "mock"

    def __init__(self, projects_dir: Path, sandbox_dir: Path, projects: list[str], *, clock: Callable[[], datetime] = _now):
        self.projects_dir = projects_dir
        self.sandbox_dir = sandbox_dir
        self.project_names = projects
        self.state_path = sandbox_dir / "tracker-state.json"
        self.clock = clock
        self._lock = threading.RLock()

    # ---------- sandbox state ----------
    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("cards", {})
                data.setdefault("comments", {})
                data.setdefault("simulate", None)
                data.setdefault("nextIssue", 1)
                data.setdefault("drift", [])
                return data
        except (OSError, ValueError):
            pass
        return {"version": 1, "cards": {}, "comments": {}, "simulate": None, "nextIssue": 1, "drift": []}

    def _save(self, data: dict[str, Any]) -> None:
        write_json_atomic(self.state_path, data)

    def simulation(self) -> str | None:
        with self._lock:
            return self._load().get("simulate")

    def set_simulation(self, code: str | None) -> None:
        if code is not None and code not in SIMULATIONS:
            raise ValueError("unknown simulation")
        with self._lock:
            data = self._load()
            data["simulate"] = code
            self._save(data)

    def mutate_content(self, req_id: str) -> None:
        """Debug: simulate ba editing a requirement (changes its hash)."""
        with self._lock:
            data = self._load()
            entry = data["cards"].setdefault(req_id, {})
            entry["contentSuffix"] = entry.get("contentSuffix", "") + f"\n\n*(แก้ไขจำลอง {_iso(self.clock())})*"
            self._save(data)

    def reset(self) -> None:
        with self._lock:
            self._save({"version": 1, "cards": {}, "comments": {}, "simulate": None, "nextIssue": 1, "drift": []})

    def _fail_reads(self, simulate: str | None) -> None:
        if simulate == "github_unavailable":
            raise TrackerError("github_unavailable", "เชื่อมต่อ GitHub ไม่ได้ชั่วคราว (จำลอง)")
        if simulate == "rate_limited":
            reset = self.clock() + timedelta(minutes=12)
            raise TrackerError("rate_limited", "GitHub จำกัดจำนวนคำขอ (จำลอง)", status=429, retry_at=_iso(reset))
        if simulate == "token_missing":
            raise TrackerError("token_missing", "ยังไม่ได้ตั้งค่า GitHub token (จำลอง)")
        if simulate == "token_expired":
            raise TrackerError("token_expired", "GitHub token หมดอายุหรือถูกเพิกถอน (จำลอง)")
        if simulate == "token_scope":
            raise TrackerError("token_scope", "GitHub token สิทธิ์ไม่พอ (จำลอง)", details={"missingScopes": ["project"]})

    # ---------- adapter contract ----------
    def health(self) -> TrackerHealth:
        simulate = self.simulation()
        self._fail_reads(simulate)
        if simulate == "token_expiring":
            expires = self.clock() + timedelta(days=5)
            return TrackerHealth(ok=True, token_expires_at=_iso(expires), warnings=[{"code": "token_expiring", "days": 5, "expiresAt": _iso(expires)}])
        return TrackerHealth()

    def _parsed(self) -> list[ParsedRequirement]:
        items: list[ParsedRequirement] = []
        for project in self.project_names:
            path = self.projects_dir / project / "requirements.md"
            if path.is_file():
                items.extend(parse_file(path, project))
        return items

    def _sync(self, data: dict[str, Any]) -> tuple[list[Card], bool]:
        """Simulated one-way sync `.veda/` -> tracker (REQ-004) inside the sandbox."""
        changed = False
        now = _iso(self.clock())
        cards: list[Card] = []
        seen: set[str] = set()
        for item in self._parsed():
            if item.req_id in seen:
                continue  # REQ-ids are unique; ignore a duplicate block
            seen.add(item.req_id)
            entry = data["cards"].get(item.req_id)
            suffix = (entry or {}).get("contentSuffix", "")
            current_hash = content_hash(item.raw_block + suffix) if suffix else item.content_hash
            if not entry or "status" not in entry:
                entry = {**(entry or {}), "status": NEEDS_REVIEW, "requestedAt": now, "issueNumber": data["nextIssue"], "syncedHash": current_hash}
                data["nextIssue"] += 1
                data["cards"][item.req_id] = entry
                changed = True
            elif entry.get("syncedHash") != current_hash:
                previous = entry.get("status")
                data["drift"] = (data.get("drift") or [])[-49:] + [{"at": now, "reqId": item.req_id, "from": entry.get("syncedHash"), "to": current_hash, "statusBefore": previous}]
                entry["syncedHash"] = current_hash
                if previous in (APPROVED, CHANGES_REQUESTED, DRAFT):
                    # Approved -> Draft -> (ba sync) Needs review, collapsed in the sandbox.
                    entry["status"] = NEEDS_REVIEW
                    entry["requestedAt"] = now
                    entry.pop("approvals", None)
                changed = True
            comments = [ReviewComment(**comment) for comment in data["comments"].get(item.req_id, [])]
            cards.append(Card(
                req_id=item.req_id,
                project=item.project,
                title=item.title,
                content_markdown=item.content_markdown + suffix,
                acceptance_criteria=item.acceptance_criteria,
                scope_in=item.scope_in,
                scope_out=item.scope_out,
                scope_source=item.scope_source,
                content_hash=current_hash,
                section_hashes=section_hashes(item, suffix),
                status=entry["status"],
                requested_at=entry.get("requestedAt"),
                issue_number=entry.get("issueNumber"),
                issue_url=None,
                issue_is_mock=True,
                group=item.group,
                comments=comments,
                approvals=list(entry.get("approvals") or []),
            ))
        return cards, changed

    def list_cards(self, project: str | None = None) -> list[Card]:
        with self._lock:
            data = self._load()
            self._fail_reads(data.get("simulate"))
            cards, changed = self._sync(data)
            if changed:
                self._save(data)
        return [card for card in cards if project is None or card.project == project]

    def get(self, req_id: str) -> Card | None:
        return next((card for card in self.list_cards() if card.req_id == req_id), None)

    def set_status(self, req_id: str, status: str, *, actor: str, content_hash: str) -> None:
        if status not in STATUSES:
            raise ValueError("unknown status")
        with self._lock:
            data = self._load()
            simulate = data.get("simulate")
            self._fail_reads(simulate)
            if simulate == "status_failed":
                raise TrackerError("github_unavailable", "ตั้ง Status ใน GitHub ไม่สำเร็จ (จำลอง)", status=502)
            entry = data["cards"].get(req_id)
            if not entry:
                raise TrackerError("conflict_state", "ไม่พบ requirement นี้ใน tracker", status=409)
            entry["status"] = status
            if status == APPROVED:
                entry["approvals"] = [*(entry.get("approvals") or []), {"actor": actor, "at": _iso(self.clock()), "hash": content_hash}]
            else:
                entry.pop("approvals", None)
            self._save(data)

    def add_comment(self, req_id: str, text: str, *, actor: str, kind: str, content_hash: str, reason: str | None = None) -> str:
        with self._lock:
            data = self._load()
            simulate = data.get("simulate")
            self._fail_reads(simulate)
            if simulate == "comment_failed":
                raise TrackerError("github_unavailable", "โพสต์คอมเมนต์ใน GitHub ไม่สำเร็จ (จำลอง)", status=502)
            comment = ReviewComment(id=f"mock-comment-{secrets.token_hex(6)}", at=_iso(self.clock()), actor=actor, kind=kind, body=text, content_hash=content_hash, reason=reason)
            data["comments"].setdefault(req_id, []).append(asdict(comment))
            self._save(data)
            return comment.id


class GitHubProjectTracker(RequirementTracker):
    """ProjectV2 "Veda Requirements" + central issue repo adapter (REQ-001, REQ-016).

    All GitHub GraphQL lives here. Not connected yet: the central repo, the
    Project and the token do not exist. Every call fails closed with a
    sanitized `TrackerError`, so the review page shows the token/setup state
    and never enables Approve.

    TODO (when the user has created the repo/Project and a token):
    - token from env `REVIEW_GITHUB_TOKEN` (loaded from ignored `.env.runtime`);
      never log, echo or send it to the browser. Confirm whether a fine-grained
      PAT can reach a *user-owned* ProjectV2; if not, classic PAT `project`+`repo`
      or move the Project to an org (REQ-016: report, do not choose silently).
    - health(): `viewer { login }` + response header `github-authentication-token-expiration`
      for expiry warnings; map 401 -> token_expired, missing scope -> token_scope,
      `x-ratelimit-reset` -> rate_limited.retry_at.
    - list_cards(): `user(login) { projectV2(number) { items(first:100) { nodes {
      id fieldValues { ...ProjectV2ItemFieldSingleSelectValue / TextValue }
      content { ... on Issue { number url body updatedAt comments } } } } } }`
      filtered by Status / REQ-ID / Veda Project fields; page through `pageInfo`.
    - get(): re-fetch a single item (no cache; queue cache <= 60s, REQ-006).
    - set_status(): `updateProjectV2ItemFieldValue` with the Status option id;
      refuse any status not in REVIEWER_STATUSES when called from the review path.
    - add_comment(): `addComment(input:{subjectId: <issue node id>, body})` -> comment id.
    - REQ-id -> issue number / item id mapping in `.veda/requirements-index.json`.
    - validate issue/PR URLs are https://github.com/<central repo>/... only.
    """

    mode = "github"

    def __init__(self, token: str | None, owner: str, project_number: int | None, repo: str):
        self._token = token
        self.owner = owner
        self.project_number = project_number
        self.repo = repo

    def _require_token(self) -> None:
        if not self._token:
            raise TrackerError("token_missing", "ยังไม่ได้ตั้งค่า GitHub token")
        raise TrackerError("github_unavailable", "ยังไม่ได้เชื่อม GitHub Project adapter (ยังไม่ implement)")

    def health(self) -> TrackerHealth:
        self._require_token()
        return TrackerHealth()

    def list_cards(self, project: str | None = None) -> list[Card]:
        self._require_token()
        return []

    def get(self, req_id: str) -> Card | None:
        self._require_token()
        return None

    def set_status(self, req_id: str, status: str, *, actor: str, content_hash: str) -> None:
        self._require_token()

    def add_comment(self, req_id: str, text: str, *, actor: str, kind: str, content_hash: str, reason: str | None = None) -> str:
        self._require_token()
        return ""
