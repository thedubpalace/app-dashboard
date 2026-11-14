"""Requirement review logic: queue, decisions, audit and the requirementsReady gate.

Talks to a `RequirementTracker` only (REQ-017). In mock mode the gate result
is written to the sandbox and reported as "would set"; registry.json is never
touched (REQ-012 is simulated until the GitHub adapter is connected).
"""
from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from services.review_markdown import render_inline, render_markdown
from services.review_tracker import (
    APPROVED, CHANGES_REQUESTED, NEEDS_REVIEW, Card, GitHubProjectTracker, MockTracker,
    RequirementTracker, TrackerError, write_json_atomic,
)

BASE_DIR = Path(__file__).resolve().parent.parent
MIN_REASON = 10
IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9_-]{12,128}")
REQ_ID = re.compile(r"REQ-\d{3,}")

# UI interpretations flagged by ux-designer and not yet confirmed by the user.
# Kept as config so they can be flipped without touching the logic.
UI_POLICY = {
    "counterShrinksOnDecision": True,   # decided cards leave the queue; total shrinks
    "skipUndoSeconds": 5,               # Skip offers a client-only undo toast
    "incompleteFields": ["reqId", "title", "contentHash", "acceptanceCriteria", "issue"],
}


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _csv(name: str, default: str) -> list[str]:
    return [item.strip() for item in os.getenv(name, default).split(",") if item.strip()]


@dataclass
class ReviewConfig:
    mode: str
    allowlist: list[str]
    required_approvals: int
    runtime_dir: Path
    audit_path: Path
    projects_dir: Path
    mock_projects: list[str]

    @classmethod
    def from_env(cls, registry_path: Path) -> "ReviewConfig":
        mode = os.getenv("REVIEW_TRACKER", "mock").strip().lower() or "mock"
        if mode not in {"mock", "github"}:
            mode = "mock"
        runtime_dir = Path(os.getenv("REVIEW_RUNTIME_DIR", str(BASE_DIR / ".runtime" / ("review-sandbox" if mode == "mock" else "review"))))
        projects_dir = Path(os.getenv("REVIEW_VEDA_PROJECTS_DIR", str(registry_path.parent)))
        default_audit = runtime_dir / "audit" / "requirement-reviews.jsonl" if mode == "mock" else projects_dir.parent / "audit" / "requirement-reviews.jsonl"
        try:
            required = max(1, int(os.getenv("REVIEW_REQUIRED_APPROVALS", "1")))
        except ValueError:
            required = 1
        return cls(
            mode=mode,
            allowlist=[name.lower() for name in _csv("REVIEW_ALLOWLIST", "thedubpalace")],
            required_approvals=required,
            runtime_dir=runtime_dir,
            audit_path=Path(os.getenv("REVIEW_AUDIT_PATH", str(default_audit))),
            projects_dir=projects_dir,
            mock_projects=_csv("REVIEW_MOCK_PROJECTS", "app-dashboard"),
        )


def build_tracker(config: ReviewConfig) -> RequirementTracker:
    if config.mode == "github":
        project_number = os.getenv("REVIEW_GITHUB_PROJECT_NUMBER")
        return GitHubProjectTracker(
            token=os.getenv("REVIEW_GITHUB_TOKEN") or None,
            owner=os.getenv("REVIEW_GITHUB_OWNER", "thedubpalace"),
            project_number=int(project_number) if project_number and project_number.isdigit() else None,
            repo=os.getenv("REVIEW_GITHUB_REPO", "thedubpalace/veda-requirements"),
        )
    return MockTracker(config.projects_dir, config.runtime_dir, config.mock_projects)


class ReviewError(Exception):
    def __init__(self, code: str, message: str, status: int, **extra: Any):
        super().__init__(message)
        self.code, self.message, self.status, self.extra = code, message, status, extra

    def payload(self) -> dict[str, Any]:
        return {"ok": False, "code": self.code, "error": self.message, **self.extra}


def tracker_error_payload(exc: TrackerError) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": False, "code": exc.code, "error": exc.message}
    if exc.retry_at:
        payload["retryAt"] = exc.retry_at
    if exc.details:
        payload["details"] = exc.details
    return payload


def missing_fields(card: Card) -> list[str]:
    missing = []
    if not card.req_id or not REQ_ID.fullmatch(card.req_id):
        missing.append("REQ-id")
    if not card.title.strip():
        missing.append("ชื่อ requirement")
    if not card.content_hash:
        missing.append("content hash")
    if not card.acceptance_criteria:
        missing.append("Acceptance criteria")
    if not card.issue_number:
        missing.append("issue")
    return missing


class ReviewService:
    def __init__(self, config: ReviewConfig, tracker: RequirementTracker, *, clock: Callable[[], str] = _iso_now):
        self.config = config
        self.tracker = tracker
        self.clock = clock
        self._lock = threading.RLock()
        self.state_path = config.runtime_dir / "service-state.json"

    # ---------- permission (REQ-014 / REQ-018) ----------
    def is_reviewer(self, username: str | None, role: str | None) -> bool:
        return bool(username) and role == "admin" and username.lower() in self.config.allowlist

    # ---------- persistence ----------
    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("intents", {})
                data.setdefault("gate", {})
                return data
        except (OSError, ValueError):
            pass
        return {"intents": {}, "gate": {}}

    def _save(self, data: dict[str, Any]) -> None:
        intents = data.get("intents", {})
        if len(intents) > 500:
            for key in sorted(intents, key=lambda name: intents[name].get("at", ""))[: len(intents) - 500]:
                intents.pop(key, None)
        write_json_atomic(self.state_path, data)

    def audit(self, entry: dict[str, Any]) -> None:
        """Append-only decision log (REQ-015). Never contains secrets or cookies."""
        self.config.audit_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"at": self.clock(), "mode": self.config.mode, **entry}, ensure_ascii=False)
        with self.config.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    # ---------- projection ----------
    def project_card(self, card: Card, cards_in_project: list[Card]) -> dict[str, Any]:
        rendered = render_markdown(card.content_markdown)
        others = [other for other in cards_in_project if other.req_id != card.req_id]
        history = []
        for comment in sorted((c for c in card.comments if c.kind == "changes_requested"), key=lambda c: c.at, reverse=True):
            history.append({"at": comment.at, "actor": comment.actor, "reason": comment.reason or "", "hash": comment.content_hash,
                            "contentChangedSinceRound": comment.content_hash != card.content_hash})
        missing = missing_fields(card)
        pending = self._load()["intents"]
        stuck = next((intent for intent in pending.values()
                      if intent.get("reqId") == card.req_id and intent.get("phase") == "comment_posted" and intent.get("hash") == card.content_hash), None)
        return {
            "reqId": card.req_id,
            "project": card.project,
            "title": card.title,
            "group": card.group,
            "status": card.status,
            "requestedAt": card.requested_at,
            "contentHtml": rendered.html,
            "contentSanitized": rendered.sanitized,
            "acceptanceCriteria": [render_inline(item).html for item in card.acceptance_criteria],
            "scope": {"in": [render_inline(item).html for item in card.scope_in],
                      "out": [render_inline(item).html for item in card.scope_out],
                      "source": card.scope_source},
            "hash": card.content_hash,
            "shortHash": card.content_hash[:7],
            "sectionHashes": card.section_hashes,
            "issue": {"number": card.issue_number, "url": card.issue_url, "mock": card.issue_is_mock},
            "prUrl": card.pr_url,
            "history": history,
            "approvals": card.approvals,
            "approvedCount": len(card.approvals),
            "requiredApprovals": self.config.required_approvals,
            "isLastOfProject": all(other.status == APPROVED for other in others),
            "incomplete": missing,
            "pendingStatusRetry": {"idempotencyKey": stuck["key"], "commentId": stuck.get("commentId")} if stuck else None,
        }

    def queue(self) -> dict[str, Any]:
        health = self.tracker.health()
        all_cards = self.tracker.list_cards()
        by_project: dict[str, list[Card]] = {}
        for card in all_cards:
            by_project.setdefault(card.project, []).append(card)
        gate = {project: self.reconcile(project, cards=cards, actor="system:open-review") for project, cards in by_project.items()}
        pending = sorted((card for card in all_cards if card.status == NEEDS_REVIEW), key=lambda card: (card.requested_at or "", card.req_id))
        return {
            "ok": True,
            "mode": self.config.mode,
            "checkedAt": self.clock(),
            "cards": [self.project_card(card, by_project[card.project]) for card in pending],
            "totals": {project: {"total": len(cards), "approved": sum(card.status == APPROVED for card in cards),
                                 "changesRequested": sum(card.status == CHANGES_REQUESTED for card in cards),
                                 "needsReview": sum(card.status == NEEDS_REVIEW for card in cards)} for project, cards in by_project.items()},
            "gate": gate,
            "warnings": health.warnings,
            "requiredApprovals": self.config.required_approvals,
            "policy": UI_POLICY,
        }

    def card(self, req_id: str) -> dict[str, Any]:
        card = self.tracker.get(req_id)
        if not card:
            raise ReviewError("not_found", "ไม่พบ requirement นี้", 404)
        return {"ok": True, "card": self.project_card(card, self.tracker.list_cards(card.project))}

    # ---------- gate (REQ-012) ----------
    def reconcile(self, project: str, *, cards: list[Card] | None = None, actor: str = "system") -> dict[str, Any]:
        cards = cards if cards is not None else self.tracker.list_cards(project)
        ready = bool(cards) and all(card.status == APPROVED for card in cards)
        approval = None
        if ready:
            approvals = [entry for card in cards for entry in card.approvals]
            approval = {"by": sorted({entry["actor"] for entry in approvals}), "at": max((entry["at"] for entry in approvals), default=self.clock()),
                        "reqIds": [card.req_id for card in cards], "hashes": {card.req_id: card.content_hash for card in cards}}
        result = {"project": project, "requirementsReady": ready, "requirementsApproval": approval}
        if self.config.mode == "mock":
            simulate = self.tracker.simulation() if isinstance(self.tracker, MockTracker) else None
            if simulate == "reconcile_failed" and actor != "system:open-review":
                raise ReviewError("reconcile_failed", "บันทึกการตัดสินแล้ว แต่ reconcile ยังไม่สำเร็จ (จำลอง)", 500)
            with self._lock:
                data = self._load()
                data["gate"][project] = {**result, "at": self.clock(), "by": actor}
                self._save(data)
            # Registry is never written in mock mode.
            return {**result, "applied": False, "target": "sandbox", "message": f"จะตั้ง requirementsReady={'true' if ready else 'false'} ให้ {project} (โหมดจำลอง — ยังไม่เขียน registry)"}
        # TODO(github mode): write only requirementsReady + requirementsApproval in registry.json
        # atomically with a lock (targeted edit, never rewrite other fields), then report.
        return {**result, "applied": False, "target": "none", "message": "ยังไม่ได้เชื่อม registry writer สำหรับโหมด GitHub"}

    # ---------- decisions ----------
    def _intent(self, key: str, action: str, req_id: str) -> dict[str, Any] | None:
        if not IDEMPOTENCY_KEY.fullmatch(key or ""):
            raise ReviewError("invalid_idempotency_key", "Idempotency-Key ไม่ถูกต้อง", 400)
        intent = self._load()["intents"].get(key)
        if intent and (intent.get("action") != action or intent.get("reqId") != req_id):
            raise ReviewError("invalid_idempotency_key", "Idempotency-Key นี้ใช้กับการกระทำอื่นไปแล้ว", 400)
        return intent

    def _store_intent(self, key: str, intent: dict[str, Any]) -> None:
        data = self._load()
        data["intents"][key] = {**intent, "key": key, "at": self.clock()}
        self._save(data)

    def _check_card(self, req_id: str) -> tuple[Card, list[Card]]:
        card = self.tracker.get(req_id)
        if not card:
            raise ReviewError("conflict_state", "ไม่พบ requirement นี้ใน tracker แล้ว", 409, currentStatus=None)
        project_cards = self.tracker.list_cards(card.project)
        return card, project_cards

    def _conflict(self, card: Card, seen_hash: str, seen_sections: dict[str, str] | None) -> None:
        if card.status != NEEDS_REVIEW:
            raise ReviewError("conflict_state", f"ใบนี้ไม่ได้อยู่ในสถานะรอรีวิวแล้ว ({card.status})", 409, currentStatus=card.status)
        if card.content_hash != seen_hash:
            labels = {"title": "ชื่อ requirement", "content": "เนื้อหา", "acceptanceCriteria": "Acceptance criteria"}
            changed = [labels[name] for name, value in card.section_hashes.items() if seen_sections and seen_sections.get(name) not in (None, value)]
            raise ReviewError("conflict_hash", "requirement นี้เปลี่ยนไปขณะที่คุณอ่าน", 409,
                              oldHash=seen_hash[:7], newHash=card.content_hash[:7], changedSections=changed, currentStatus=card.status)

    def _issue_url(self, card: Card) -> str | None:
        return card.issue_url or (f"mock://issue/{card.issue_number}" if card.issue_number else None)

    def _post_write_simulation(self) -> None:
        if isinstance(self.tracker, MockTracker) and self.tracker.simulation() == "network_unknown":
            raise ReviewError("network_unknown", "บันทึกอาจสำเร็จแล้ว แต่ไม่ได้รับการยืนยันจาก GitHub", 504)

    def _reconcile_after(self, project: str, actor: str) -> dict[str, Any]:
        try:
            return self.reconcile(project, actor=actor)
        except ReviewError as exc:
            return {"project": project, "error": exc.message, "code": exc.code}

    def approve(self, *, actor: str, role: str, session_id: str, req_id: str, seen_hash: str, seen_sections: dict[str, str] | None, key: str) -> dict[str, Any]:
        with self._lock:
            intent = self._intent(key, "approve", req_id)
            if intent and intent.get("phase") == "done":
                return {**intent["response"], "replayed": True}
            card, project_cards = self._check_card(req_id)
            if card.status == APPROVED and card.content_hash == seen_hash:
                # Another tab or a retried request already approved this exact content (REQ-007 a).
                response = {"ok": True, "reqId": req_id, "alreadyApproved": True, "status": APPROVED,
                            "reconcile": self._reconcile_after(card.project, actor)}
                self._store_intent(key, {"action": "approve", "reqId": req_id, "phase": "done", "response": response})
                return response
            self._conflict(card, seen_hash, seen_sections)
            missing = missing_fields(card)
            if missing:
                raise ReviewError("card_incomplete", f"ใบนี้ข้อมูลไม่ครบ ({', '.join(missing)})", 422, missing=missing)
            others = [other for other in project_cards if other.req_id != req_id]
            is_last = all(other.status == APPROVED for other in others)
            base = {"actor": actor, "role": role, "sessionId": session_id, "action": "approve", "reqId": req_id, "project": card.project,
                    "statusBefore": card.status, "contentHash": card.content_hash, "issueUrl": self._issue_url(card), "idempotencyKey": key}
            try:
                self.tracker.set_status(req_id, APPROVED, actor=actor, content_hash=card.content_hash)
            except TrackerError as exc:
                self.audit({**base, "statusAfter": card.status, "result": "failed", "error": f"{exc.code}: {exc.message}"})
                raise
            comment_id = None
            warning = None
            try:
                comment_id = self.tracker.add_comment(req_id, f"Approved by @{actor} via Veda Dashboard (hash {card.content_hash[:7]})",
                                                      actor=actor, kind="approved", content_hash=card.content_hash)
            except TrackerError as exc:
                warning = "อนุมัติแล้ว แต่โพสต์คอมเมนต์ยืนยันบน issue ไม่สำเร็จ"
                comment_id = None
                base["commentError"] = exc.code
            self.audit({**base, "statusAfter": APPROVED, "commentId": comment_id, "result": "succeeded"})
            reconcile = self._reconcile_after(card.project, actor)
            response = {"ok": True, "reqId": req_id, "status": APPROVED, "isLastOfProject": is_last, "commentId": comment_id,
                        "reconcile": reconcile, "warning": warning}
            self._store_intent(key, {"action": "approve", "reqId": req_id, "phase": "done", "response": response})
            self._post_write_simulation()
            return response

    def request_changes(self, *, actor: str, role: str, session_id: str, req_id: str, seen_hash: str, seen_sections: dict[str, str] | None, reason: str, key: str) -> dict[str, Any]:
        reason = (reason or "").strip()
        with self._lock:
            intent = self._intent(key, "request_changes", req_id)
            if intent and intent.get("phase") == "done":
                return {**intent["response"], "replayed": True}
            if intent and intent.get("phase") == "comment_posted":
                # Two-phase retry: the comment already carries the reason; never re-post or change it.
                reason = str(intent.get("reason") or reason)
            if len(reason) < MIN_REASON:
                raise ReviewError("reason_too_short", f"ต้องใส่เหตุผลอย่างน้อย {MIN_REASON} ตัวอักษร", 400, minLength=MIN_REASON)
            if len(reason) > 5000:
                raise ReviewError("reason_too_long", "เหตุผลยาวเกิน 5000 ตัวอักษร", 400)
            card, _ = self._check_card(req_id)
            comment_id = intent.get("commentId") if intent and intent.get("phase") == "comment_posted" else None
            base = {"actor": actor, "role": role, "sessionId": session_id, "action": "request_changes", "reqId": req_id, "project": card.project,
                    "statusBefore": card.status, "contentHash": seen_hash, "issueUrl": self._issue_url(card), "reason": reason, "idempotencyKey": key}
            if comment_id is None:
                if card.status == CHANGES_REQUESTED and card.content_hash == seen_hash:
                    response = {"ok": True, "reqId": req_id, "status": CHANGES_REQUESTED, "alreadyRequested": True,
                                "reconcile": self._reconcile_after(card.project, actor)}
                    self._store_intent(key, {"action": "request_changes", "reqId": req_id, "phase": "done", "response": response})
                    return response
                self._conflict(card, seen_hash, seen_sections)
                body = (f"Changes requested for {req_id} by @{actor} via Veda Dashboard\n"
                        f"Reviewed at: {self.clock()}\nContent hash: {card.content_hash[:12]}\n\nReason:\n{reason}")
                try:
                    comment_id = self.tracker.add_comment(req_id, body, actor=actor, kind="changes_requested", content_hash=card.content_hash, reason=reason)
                except TrackerError as exc:
                    # Comment failed: status must not change (REQ-008 b).
                    self.audit({**base, "statusAfter": card.status, "result": "failed", "phase": "comment", "error": f"{exc.code}: {exc.message}"})
                    raise
                self._store_intent(key, {"action": "request_changes", "reqId": req_id, "phase": "comment_posted", "commentId": comment_id, "hash": card.content_hash, "reason": reason})
            elif card.status == CHANGES_REQUESTED:
                response = {"ok": True, "reqId": req_id, "status": CHANGES_REQUESTED, "commentId": comment_id, "reconcile": self._reconcile_after(card.project, actor)}
                self._store_intent(key, {"action": "request_changes", "reqId": req_id, "phase": "done", "response": response})
                return response
            elif card.status != NEEDS_REVIEW:
                raise ReviewError("conflict_state", f"ใบนี้ไม่ได้อยู่ในสถานะรอรีวิวแล้ว ({card.status})", 409, currentStatus=card.status)
            try:
                self.tracker.set_status(req_id, CHANGES_REQUESTED, actor=actor, content_hash=card.content_hash)
            except TrackerError as exc:
                self.audit({**base, "statusAfter": card.status, "commentId": comment_id, "result": "failed", "phase": "status", "error": f"{exc.code}: {exc.message}"})
                raise ReviewError("status_failed", "คอมเมนต์โพสต์แล้ว แต่ยังตั้งสถานะไม่สำเร็จ", 502, commentPosted=True, commentId=comment_id, cause=exc.code) from exc
            self.audit({**base, "statusAfter": CHANGES_REQUESTED, "commentId": comment_id, "result": "succeeded"})
            reconcile = self._reconcile_after(card.project, actor)
            response = {"ok": True, "reqId": req_id, "status": CHANGES_REQUESTED, "commentId": comment_id, "reconcile": reconcile}
            self._store_intent(key, {"action": "request_changes", "reqId": req_id, "phase": "done", "response": response})
            self._post_write_simulation()
            return response
