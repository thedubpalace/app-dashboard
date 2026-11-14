"""Requirement Review Queue (REQ-001..018, mock tracker) — API, permission, gate, XSS."""
from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import threading
import time
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

import main
from services import review_api
from services.dashboard_auth import create_session
from services.review_markdown import ALLOWED_ATTRS, ALLOWED_TAGS, render_inline, render_markdown
from services.review_parser import parse_requirements
from services.review_service import ReviewConfig, ReviewService
from services.review_tracker import (
    APPROVED, CHANGES_REQUESTED, NEEDS_REVIEW, Card, MockTracker, RequirementTracker, TrackerError, TrackerHealth,
)

REQUIREMENTS = """# Demo

## Scope

### อยู่ใน v1
- หน้า review

### นอก v1
- Jira adapter

## Requirements

### กลุ่ม A

**REQ-101 First requirement**
- ทำสิ่งแรก
- AC: (a) ข้อแรก (b) ข้อสอง

**REQ-102 Second requirement**
- ทำสิ่งที่สอง <script>alert(1)</script>
- AC: (a) ข้อเดียว

## Notes
- not part of a REQ
"""
SECRET_TOKEN = "ghp_SUPERSECRETTOKENVALUE1234567890"


def call(method: str, path: str, *, cookie: str | None = None, headers: dict[str, str] | None = None, body: dict | None = None):
    raw = json.dumps(body).encode() if body is not None else b""
    hdrs = [(b"host", b"testserver")]
    for key, value in (headers or {}).items():
        hdrs.append((key.lower().encode(), value.encode()))
    if cookie:
        hdrs.append((b"cookie", f"veda_dashboard_session={cookie}".encode()))
    if body is not None:
        hdrs.append((b"content-type", b"application/json"))
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http", "path": path,
             "raw_path": path.encode(), "query_string": b"", "root_path": "", "headers": hdrs, "client": ("127.0.0.1", 5000), "server": ("testserver", 80)}
    messages: list[dict] = []
    delivered = False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": raw, "more_body": False}
        await asyncio.sleep(3600)

    async def send(message):
        messages.append(message)

    asyncio.run(main.app(scope, receive, send))
    start = next(m for m in messages if m["type"] == "http.response.start")
    payload = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    response_headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    try:
        data = json.loads(payload)
    except ValueError:
        data = payload.decode("utf-8", "replace")
    return start["status"], data, response_headers, payload


class ReviewApiTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.projects = root / "veda" / ".veda" / "projects"
        (self.projects / "demo").mkdir(parents=True)
        self.req_path = self.projects / "demo" / "requirements.md"
        self.req_path.write_text(REQUIREMENTS, encoding="utf-8")
        self.registry = self.projects / "registry.json"
        self.registry.write_text(json.dumps({"projects": [{"name": "demo", "requirementsReady": False}]}, indent=2), encoding="utf-8")
        self.registry_bytes = self.registry.read_bytes()
        self.runtime = root / "runtime"
        self.security_log = root / "security.log"
        self.env = {
            "VEDA_DASHBOARD_SESSION_SECRET": "session-secret",
            "VEDA_DASHBOARD_USERNAME": "thedubpalace",
            "VEDA_DASHBOARD_PASSWORD": "pw",
            "REVIEW_TRACKER": "mock",
            "REVIEW_VEDA_PROJECTS_DIR": str(self.projects),
            "REVIEW_RUNTIME_DIR": str(self.runtime),
            "REVIEW_MOCK_PROJECTS": "demo",
            "DASHBOARD_SECURITY_LOG": str(self.security_log),
        }
        for name in ("DASHBOARD_USER_ROLES", "REVIEW_ALLOWLIST", "REVIEW_AUDIT_PATH", "REVIEW_GITHUB_TOKEN", "REVIEW_REQUIRED_APPROVALS"):
            self.env.setdefault(name, "")
        self.patcher = patch.dict(os.environ, self.env)
        self.patcher.start()
        for name in ("DASHBOARD_USER_ROLES", "REVIEW_ALLOWLIST", "REVIEW_AUDIT_PATH", "REVIEW_GITHUB_TOKEN", "REVIEW_REQUIRED_APPROVALS"):
            os.environ.pop(name, None)
        review_api._services.clear()

    def tearDown(self):
        self.patcher.stop()
        review_api._services.clear()
        self.tmp.cleanup()

    # ---------- helpers ----------
    def login(self, username: str = "thedubpalace") -> tuple[str, str]:
        return create_session(username)

    def queue(self, cookie):
        status, data, _, _ = call("GET", "/api/review/queue", cookie=cookie)
        self.assertEqual(status, 200, data)
        return data

    def decide(self, action: str, req_id: str, card: dict, cookie: str, csrf: str, key: str, reason: str | None = None):
        body = {"hash": card["hash"], "sectionHashes": card["sectionHashes"]}
        if reason is not None:
            body["reason"] = reason
        return call("POST", f"/api/review/cards/{req_id}/{action}", cookie=cookie, headers={"x-csrf-token": csrf, "idempotency-key": key}, body=body)

    def audit_lines(self):
        path = self.runtime / "audit" / "requirement-reviews.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def tracker(self) -> MockTracker:
        return review_api.get_service().tracker

    def assertRegistryUntouched(self):
        self.assertEqual(self.registry.read_bytes(), self.registry_bytes)


class PermissionTests(ReviewApiTestCase):
    def test_no_session_is_401_and_logged(self):
        for method, path in (("GET", "/api/review/queue"), ("GET", "/api/review/cards/REQ-101"), ("GET", "/api/review/summary"),
                             ("POST", "/api/review/cards/REQ-101/approve"), ("POST", "/api/review/cards/REQ-101/request-changes"),
                             ("POST", "/api/review/reconcile"), ("POST", "/api/review/debug"), ("GET", "/api/review/debug")):
            status, data, _, _ = call(method, path, body={} if method == "POST" else None)
            self.assertEqual(status, 401, path)
            self.assertEqual(data["code"], "unauthenticated")
        entries = [json.loads(line) for line in self.security_log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(entries), 8)
        self.assertTrue(all(entry["status"] == 401 and entry["permission"] == "requirement.review" for entry in entries))

    def test_expired_or_tampered_cookie_reports_session_expired(self):
        cookie, _ = self.login()
        status, data, _, _ = call("GET", "/api/review/queue", cookie=cookie.replace("thedubpalace", "mallory", 1))
        self.assertEqual((status, data["code"]), (401, "session_expired"))
        with patch("services.dashboard_auth.time.time", return_value=time.time() - 13 * 3600):
            old_cookie, _ = self.login()
        status, data, _, _ = call("GET", "/api/review/queue", cookie=old_cookie)
        self.assertEqual((status, data["code"]), (401, "session_expired"))

    def test_viewer_operator_and_non_allowlisted_admin_are_403(self):
        os.environ["DASHBOARD_USER_ROLES"] = "thedubpalace:admin,bob:viewer,olga:operator,carol:admin"
        for user in ("bob", "olga", "carol"):
            cookie, _ = self.login(user)
            status, data, _, body = call("GET", "/api/review/queue", cookie=cookie)
            self.assertEqual((status, data["code"]), (403, "forbidden"), user)
            self.assertNotIn(b"thedubpalace", body)  # allowlist is not disclosed
        self.assertEqual(self.queue(self.login()[0])["reviewer"], {"username": "thedubpalace", "role": "admin"})

    def test_allowlisted_user_must_also_be_admin(self):
        os.environ["DASHBOARD_USER_ROLES"] = "thedubpalace:operator"
        status, data, _, _ = call("GET", "/api/review/queue", cookie=self.login()[0])
        self.assertEqual(status, 403)

    def test_adding_admin_to_allowlist_needs_no_code_change(self):
        os.environ["DASHBOARD_USER_ROLES"] = "thedubpalace:admin,carol:admin"
        os.environ["REVIEW_ALLOWLIST"] = "thedubpalace,carol"
        cookie, csrf = self.login("carol")
        card = self.queue(cookie)["cards"][0]
        status, data, _, _ = self.decide("approve", card["reqId"], card, cookie, csrf, "approve_carol_000001")
        self.assertEqual(status, 200, data)
        self.assertEqual(self.audit_lines()[-1]["actor"], "carol")

    def test_mutations_require_csrf(self):
        cookie, csrf = self.login()
        card = self.queue(cookie)["cards"][0]
        for token in ("", "wrong"):
            status, data, _, _ = call("POST", f"/api/review/cards/{card['reqId']}/approve", cookie=cookie,
                                      headers={"x-csrf-token": token, "idempotency-key": "approve_csrf_0000001"}, body={"hash": card["hash"]})
            self.assertEqual((status, data["code"]), (403, "csrf_invalid"))
            status, data, _, _ = call("POST", "/api/review/debug", cookie=cookie, headers={"x-csrf-token": token}, body={"action": "reset"})
            self.assertEqual(status, 403)
        self.assertEqual(self.tracker().get(card["reqId"]).status, NEEDS_REVIEW)
        self.assertEqual(self.audit_lines(), [])

    def test_review_page_is_served_with_security_headers_and_mock_banner(self):
        status, body, headers, _ = call("GET", "/review")
        self.assertEqual(status, 200)
        self.assertIn("ข้อมูลจำลอง — ยังไม่เชื่อม GitHub", body)
        self.assertIn("frame-ancestors 'none'", headers["content-security-policy"])
        self.assertEqual(headers["cache-control"], "no-store")


class QueueTests(ReviewApiTestCase):
    def test_queue_lists_needs_review_cards_oldest_first_with_all_fields(self):
        data = self.queue(self.login()[0])
        self.assertEqual(data["mode"], "mock")
        self.assertEqual([card["reqId"] for card in data["cards"]], ["REQ-101", "REQ-102"])
        card = data["cards"][0]
        for field in ("title", "project", "contentHtml", "acceptanceCriteria", "scope", "hash", "shortHash", "issue", "requestedAt", "history",
                      "isLastOfProject", "approvedCount", "requiredApprovals", "incomplete"):
            self.assertIn(field, card)
        self.assertEqual(card["acceptanceCriteria"], ["ข้อแรก", "ข้อสอง"])
        self.assertEqual(card["scope"]["source"], "feature")
        self.assertEqual(card["issue"]["mock"], True)
        self.assertEqual(card["incomplete"], [])
        self.assertFalse(card["isLastOfProject"])
        self.assertTrue(data["cards"][1]["contentSanitized"])
        self.assertNotIn("<script", data["cards"][1]["contentHtml"])
        self.assertRegistryUntouched()

    def test_non_needs_review_cards_are_hidden(self):
        cookie, csrf = self.login()
        card = self.queue(cookie)["cards"][0]
        self.decide("approve", card["reqId"], card, cookie, csrf, "approve_hidden_00001")
        self.assertEqual([c["reqId"] for c in self.queue(cookie)["cards"]], ["REQ-102"])

    def test_card_without_acceptance_criteria_is_incomplete_and_cannot_be_approved(self):
        self.req_path.write_text(REQUIREMENTS.replace("- AC: (a) ข้อเดียว\n", ""), encoding="utf-8")
        cookie, csrf = self.login()
        card = self.queue(cookie)["cards"][1]
        self.assertEqual(card["incomplete"], ["Acceptance criteria"])
        status, data, _, _ = self.decide("approve", card["reqId"], card, cookie, csrf, "approve_incomp_00001")
        self.assertEqual((status, data["code"]), (422, "card_incomplete"))
        self.assertEqual(self.tracker().get("REQ-102").status, NEEDS_REVIEW)


class ApproveTests(ReviewApiTestCase):
    def test_approve_writes_status_comment_audit_and_is_idempotent(self):
        cookie, csrf = self.login()
        card = self.queue(cookie)["cards"][0]
        status, data, _, _ = self.decide("approve", "REQ-101", card, cookie, csrf, "approve_once_0000001")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["status"], APPROVED)
        self.assertEqual(self.tracker().get("REQ-101").status, APPROVED)
        comments = self.tracker().get("REQ-101").comments
        self.assertEqual([c.kind for c in comments], ["approved"])
        self.assertIn("Approved by @thedubpalace via Veda Dashboard", comments[0].body)
        # Same key (double tap) and a second tab (new key) both succeed without a second approval.
        status, again, _, _ = self.decide("approve", "REQ-101", card, cookie, csrf, "approve_once_0000001")
        self.assertEqual(status, 200); self.assertTrue(again["replayed"])
        status, other_tab, _, _ = self.decide("approve", "REQ-101", card, cookie, csrf, "approve_tab2_0000001")
        self.assertEqual(status, 200); self.assertTrue(other_tab["alreadyApproved"])
        lines = self.audit_lines()
        self.assertEqual(len(lines), 1)
        entry = lines[0]
        for field in ("at", "actor", "role", "sessionId", "action", "reqId", "project", "statusBefore", "statusAfter", "contentHash", "issueUrl", "commentId", "result"):
            self.assertIn(field, entry)
        self.assertEqual((entry["actor"], entry["role"], entry["action"], entry["statusBefore"], entry["statusAfter"], entry["result"]),
                         ("thedubpalace", "admin", "approve", NEEDS_REVIEW, APPROVED, "succeeded"))
        self.assertNotIn(cookie, json.dumps(entry))
        self.assertEqual(len(self.tracker().get("REQ-101").comments), 1)
        self.assertRegistryUntouched()

    def test_concurrent_approvals_with_different_keys_approve_once(self):
        cookie, csrf = self.login()
        card = self.queue(cookie)["cards"][0]
        results = []
        threads = [threading.Thread(target=lambda i=i: results.append(self.decide("approve", "REQ-101", card, cookie, csrf, f"approve_race_{i:07d}")[0])) for i in range(4)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(results, [200] * 4)
        self.assertEqual(len([line for line in self.audit_lines() if line["result"] == "succeeded"]), 1)

    def test_content_change_while_reading_is_409_conflict_hash(self):
        cookie, csrf = self.login()
        card = self.queue(cookie)["cards"][0]
        self.req_path.write_text(REQUIREMENTS.replace("(b) ข้อสอง", "(b) ข้อสองที่แก้แล้ว"), encoding="utf-8")
        status, data, _, _ = self.decide("approve", "REQ-101", card, cookie, csrf, "approve_conflict_001")
        self.assertEqual((status, data["code"]), (409, "conflict_hash"))
        self.assertEqual(data["oldHash"], card["hash"][:7])
        self.assertNotEqual(data["newHash"], data["oldHash"])
        self.assertEqual(data["changedSections"], ["Acceptance criteria"])
        self.assertEqual(self.tracker().get("REQ-101").status, NEEDS_REVIEW)
        self.assertEqual(self.audit_lines(), [])

    def test_debug_mutation_also_triggers_conflict(self):
        cookie, csrf = self.login()
        card = self.queue(cookie)["cards"][0]
        status, _, _, _ = call("POST", "/api/review/debug", cookie=cookie, headers={"x-csrf-token": csrf}, body={"action": "mutate", "reqId": "REQ-101"})
        self.assertEqual(status, 200)
        status, data, _, _ = self.decide("approve", "REQ-101", card, cookie, csrf, "approve_mutate_00001")
        self.assertEqual((status, data["code"], data["changedSections"]), (409, "conflict_hash", ["เนื้อหา"]))

    def test_decision_on_card_no_longer_in_review_is_409_conflict_state(self):
        cookie, csrf = self.login()
        card = self.queue(cookie)["cards"][0]
        self.decide("request-changes", "REQ-101", card, cookie, csrf, "changes_first_00001", reason="ต้องเพิ่มรายละเอียดอีก")
        status, data, _, _ = self.decide("approve", "REQ-101", card, cookie, csrf, "approve_state_000001")
        self.assertEqual((status, data["code"], data["currentStatus"]), (409, "conflict_state", CHANGES_REQUESTED))

    def test_tracker_failure_keeps_status_and_records_failed_audit(self):
        cookie, csrf = self.login()
        card = self.queue(cookie)["cards"][0]
        self.tracker().set_simulation("github_unavailable")
        status, data, _, _ = self.decide("approve", "REQ-101", card, cookie, csrf, "approve_down_0000001")
        self.assertEqual((status, data["code"]), (503, "github_unavailable"))
        self.tracker().set_simulation(None)
        self.assertEqual(self.tracker().get("REQ-101").status, NEEDS_REVIEW)
        status, data, _, _ = call("GET", "/api/review/queue", cookie=cookie)
        self.assertFalse(data["gate"]["demo"]["requirementsReady"])

    def test_rate_limit_reports_reset_time(self):
        cookie, _ = self.login()
        self.tracker().set_simulation("rate_limited")
        status, data, _, _ = call("GET", "/api/review/queue", cookie=cookie)
        self.assertEqual((status, data["code"]), (429, "rate_limited"))
        self.assertRegex(data["retryAt"], r"^\d{4}-\d\d-\d\dT")

    def test_invalid_idempotency_key_is_rejected(self):
        cookie, csrf = self.login()
        card = self.queue(cookie)["cards"][0]
        status, data, _, _ = self.decide("approve", "REQ-101", card, cookie, csrf, "bad key!")
        self.assertEqual((status, data["code"]), (400, "invalid_idempotency_key"))

    def test_network_unknown_still_records_once_and_status_check_sees_approved(self):
        cookie, csrf = self.login()
        card = self.queue(cookie)["cards"][0]
        self.tracker().set_simulation("network_unknown")
        status, data, _, _ = self.decide("approve", "REQ-101", card, cookie, csrf, "approve_unknown_0001")
        self.assertEqual((status, data["code"]), (504, "network_unknown"))
        self.tracker().set_simulation(None)
        status, data, _, _ = call("GET", "/api/review/cards/REQ-101", cookie=cookie)
        self.assertEqual(data["card"]["status"], APPROVED)
        status, replay, _, _ = self.decide("approve", "REQ-101", card, cookie, csrf, "approve_unknown_0001")
        self.assertEqual(status, 200)
        self.assertEqual(len(self.audit_lines()), 1)


class GateTests(ReviewApiTestCase):
    def approve_all(self, cookie, csrf):
        for index, card in enumerate(self.queue(cookie)["cards"]):
            status, data, _, _ = self.decide("approve", card["reqId"], card, cookie, csrf, f"approve_all_{index:08d}")
            self.assertEqual(status, 200, data)
        return data

    def test_last_approval_would_set_ready_in_sandbox_only(self):
        cookie, csrf = self.login()
        cards = self.queue(cookie)["cards"]
        self.decide("approve", "REQ-101", cards[0], cookie, csrf, "approve_gate_0000001")
        last = self.queue(cookie)["cards"][0]
        self.assertTrue(last["isLastOfProject"])
        status, data, _, _ = self.decide("approve", "REQ-102", last, cookie, csrf, "approve_gate_0000002")
        self.assertTrue(data["isLastOfProject"])
        self.assertEqual(data["reconcile"]["requirementsReady"], True)
        self.assertEqual(data["reconcile"]["applied"], False)
        self.assertEqual(data["reconcile"]["target"], "sandbox")
        self.assertIn("จะตั้ง requirementsReady=true", data["reconcile"]["message"])
        approval = data["reconcile"]["requirementsApproval"]
        self.assertEqual((approval["by"], approval["reqIds"]), (["thedubpalace"], ["REQ-101", "REQ-102"]))
        state = json.loads((self.runtime / "service-state.json").read_text(encoding="utf-8"))
        self.assertTrue(state["gate"]["demo"]["requirementsReady"])
        self.assertRegistryUntouched()

    def test_content_change_after_approval_reopens_review_and_clears_ready(self):
        cookie, csrf = self.login()
        self.approve_all(cookie, csrf)
        self.req_path.write_text(REQUIREMENTS.replace("ทำสิ่งแรก", "ทำสิ่งแรกแบบใหม่"), encoding="utf-8")
        data = self.queue(cookie)
        self.assertEqual([c["reqId"] for c in data["cards"]], ["REQ-101"])
        self.assertFalse(data["gate"]["demo"]["requirementsReady"])
        self.assertRegistryUntouched()

    def test_request_changes_forces_not_ready(self):
        cookie, csrf = self.login()
        cards = self.queue(cookie)["cards"]
        self.decide("approve", "REQ-101", cards[0], cookie, csrf, "approve_nr_000000001")
        status, data, _, _ = self.decide("request-changes", "REQ-102", cards[1], cookie, csrf, "changes_nr_000000001", reason="  เพิ่ม AC ข้อสอง  ")
        self.assertEqual(status, 200, data)
        self.assertFalse(data["reconcile"]["requirementsReady"])

    def test_reconcile_failure_after_decision_is_reported_and_repairable(self):
        cookie, csrf = self.login()
        card = self.queue(cookie)["cards"][0]
        self.tracker().set_simulation("reconcile_failed")
        status, data, _, _ = self.decide("approve", "REQ-101", card, cookie, csrf, "approve_recfail_0001")
        self.assertEqual(status, 200)
        self.assertEqual(data["reconcile"]["code"], "reconcile_failed")
        self.tracker().set_simulation(None)
        status, data, _, _ = call("POST", "/api/review/reconcile", cookie=cookie, headers={"x-csrf-token": csrf}, body={"project": "demo"})
        self.assertEqual(status, 200)
        self.assertFalse(data["reconcile"]["requirementsReady"])


class RequestChangesTests(ReviewApiTestCase):
    def test_reason_is_required_server_side_after_trim(self):
        cookie, csrf = self.login()
        card = self.queue(cookie)["cards"][0]
        for reason in ("", "   ", "สั้นไป", "         a         "):
            status, data, _, _ = self.decide("request-changes", "REQ-101", card, cookie, csrf, "changes_short_000001", reason=reason)
            self.assertEqual((status, data["code"]), (400, "reason_too_short"), reason)
        self.assertEqual(self.tracker().get("REQ-101").comments, [])

    def test_comment_is_posted_before_status_and_appears_in_history(self):
        cookie, csrf = self.login()
        card = self.queue(cookie)["cards"][0]
        status, data, _, _ = self.decide("request-changes", "REQ-101", card, cookie, csrf, "changes_ok_000000001", reason="กรุณาเพิ่มเงื่อนไข error")
        self.assertEqual(status, 200, data)
        stored = self.tracker().get("REQ-101")
        self.assertEqual(stored.status, CHANGES_REQUESTED)
        comment = stored.comments[0]
        for part in ("REQ-101", "@thedubpalace", card["hash"][:12], "กรุณาเพิ่มเงื่อนไข error"):
            self.assertIn(part, comment.body)
        entry = self.audit_lines()[0]
        self.assertEqual((entry["action"], entry["statusAfter"], entry["reason"], entry["commentId"]), ("request_changes", CHANGES_REQUESTED, "กรุณาเพิ่มเงื่อนไข error", comment.id))
        # ba edits the requirement -> simulated sync -> back in the queue with history.
        self.req_path.write_text(REQUIREMENTS.replace("ทำสิ่งแรก", "ทำสิ่งแรกพร้อม error"), encoding="utf-8")
        # Re-queued with a fresh requestedAt, so it may sort after REQ-102 once a second has passed.
        again = next(item for item in self.queue(cookie)["cards"] if item["reqId"] == "REQ-101")
        self.assertEqual(again["history"][0]["reason"], "กรุณาเพิ่มเงื่อนไข error")
        self.assertTrue(again["history"][0]["contentChangedSinceRound"])

    def test_failed_comment_leaves_status_unchanged(self):
        cookie, csrf = self.login()
        card = self.queue(cookie)["cards"][0]
        self.tracker().set_simulation("comment_failed")
        status, data, _, _ = self.decide("request-changes", "REQ-101", card, cookie, csrf, "changes_cfail_000001", reason="เหตุผลยาวพอแล้วนะ")
        self.assertEqual(status, 502)
        self.tracker().set_simulation(None)
        self.assertEqual(self.tracker().get("REQ-101").status, NEEDS_REVIEW)
        self.assertEqual(self.tracker().get("REQ-101").comments, [])
        self.assertEqual(self.audit_lines()[0]["result"], "failed")

    def test_two_phase_retry_sets_status_without_reposting_comment(self):
        cookie, csrf = self.login()
        card = self.queue(cookie)["cards"][0]
        self.tracker().set_simulation("status_failed")
        status, data, _, _ = self.decide("request-changes", "REQ-101", card, cookie, csrf, "changes_2phase_00001", reason="เพิ่มรายละเอียด rollback")
        self.assertEqual((status, data["code"], data["commentPosted"]), (502, "status_failed", True))
        self.assertEqual(self.tracker().get("REQ-101").status, NEEDS_REVIEW)
        # The card advertises the stuck retry so the UI can disable Approve and offer a retry.
        stuck = self.queue(cookie)["cards"][0]
        self.assertEqual(stuck["pendingStatusRetry"]["idempotencyKey"], "changes_2phase_00001")
        self.tracker().set_simulation(None)
        status, data, _, _ = self.decide("request-changes", "REQ-101", card, cookie, csrf, "changes_2phase_00001", reason="")
        self.assertEqual(status, 200, data)
        stored = self.tracker().get("REQ-101")
        self.assertEqual(stored.status, CHANGES_REQUESTED)
        self.assertEqual(len(stored.comments), 1)
        self.assertEqual(self.audit_lines()[-1]["reason"], "เพิ่มรายละเอียด rollback")


class TokenAndModeTests(ReviewApiTestCase):
    def test_github_mode_without_token_fails_closed(self):
        os.environ["REVIEW_TRACKER"] = "github"
        cookie, csrf = self.login()
        status, data, _, _ = call("GET", "/api/review/queue", cookie=cookie)
        self.assertEqual((status, data["code"], data["mode"]), (503, "token_missing", "github"))
        status, data, _, _ = call("POST", "/api/review/debug", cookie=cookie, headers={"x-csrf-token": csrf}, body={"action": "reset"})
        self.assertEqual(status, 404)

    def test_token_never_reaches_responses_html_or_js(self):
        os.environ["REVIEW_TRACKER"] = "github"
        os.environ["REVIEW_GITHUB_TOKEN"] = SECRET_TOKEN
        cookie, csrf = self.login()
        bodies = [call("GET", path, cookie=cookie)[3] for path in ("/api/review/queue", "/api/review/summary", "/review", "/static/review.js", "/static/review-model.js", "/")]
        bodies.append(call("POST", "/api/review/cards/REQ-101/approve", cookie=cookie, headers={"x-csrf-token": csrf, "idempotency-key": "approve_token_000001"}, body={"hash": "x"})[3])
        for body in bodies:
            self.assertNotIn(SECRET_TOKEN.encode(), body)

    def test_simulated_token_states_have_distinct_codes(self):
        cookie, _ = self.login()
        for code in ("token_missing", "token_expired", "token_scope", "github_unavailable"):
            self.tracker().set_simulation(code)
            status, data, _, _ = call("GET", "/api/review/queue", cookie=cookie)
            self.assertEqual(data["code"], code)
        self.tracker().set_simulation("token_expiring")
        data = self.queue(cookie)
        self.assertEqual(data["warnings"][0]["code"], "token_expiring")

    def test_mock_mode_audit_stays_in_sandbox(self):
        cookie, csrf = self.login()
        card = self.queue(cookie)["cards"][0]
        self.decide("approve", "REQ-101", card, cookie, csrf, "approve_sandbox_0001")
        self.assertTrue((self.runtime / "audit" / "requirement-reviews.jsonl").exists())
        self.assertFalse((self.projects.parent / "audit").exists())


class FakeTracker(RequirementTracker):
    """In-memory tracker: review logic must run without GitHub (REQ-017 b)."""

    mode = "fake"

    def __init__(self):
        item = parse_requirements(REQUIREMENTS, "demo")[0]
        self.card = Card(req_id=item.req_id, project="demo", title=item.title, content_markdown=item.content_markdown,
                         acceptance_criteria=item.acceptance_criteria, scope_in=[], scope_out=[], scope_source="none",
                         content_hash=item.content_hash, section_hashes={}, status=NEEDS_REVIEW, requested_at="2026-11-14T00:00:00Z",
                         issue_number=7, issue_url="https://github.com/thedubpalace/veda-requirements/issues/7", issue_is_mock=False)
        self.calls: list[str] = []

    def health(self):
        return TrackerHealth()

    def list_cards(self, project=None):
        return [self.card]

    def get(self, req_id):
        return self.card if req_id == self.card.req_id else None

    def set_status(self, req_id, status, *, actor, content_hash):
        self.calls.append(f"status:{status}")
        self.card.status = status
        self.card.approvals = [{"actor": actor, "at": "2026-11-14T00:00:01Z", "hash": content_hash}] if status == APPROVED else []

    def add_comment(self, req_id, text, *, actor, kind, content_hash, reason=None):
        self.calls.append(f"comment:{kind}")
        return "c1"


class TrackerBoundaryTests(unittest.TestCase):
    def test_review_logic_runs_against_fake_tracker(self):
        with tempfile.TemporaryDirectory() as folder:
            config = ReviewConfig(mode="github", allowlist=["thedubpalace"], required_approvals=1, runtime_dir=Path(folder),
                                  audit_path=Path(folder) / "audit.jsonl", projects_dir=Path(folder), mock_projects=[])
            fake = FakeTracker()
            service = ReviewService(config, fake)
            result = service.approve(actor="thedubpalace", role="admin", session_id="s", req_id="REQ-101", seen_hash=fake.card.content_hash,
                                     seen_sections=None, key="approve_fake_0000001")
            self.assertEqual(result["status"], APPROVED)
            self.assertEqual(fake.calls, ["status:Approved", "comment:approved"])
            self.assertTrue(result["reconcile"]["requirementsReady"])
            fake.card.status = NEEDS_REVIEW
            service.request_changes(actor="thedubpalace", role="admin", session_id="s", req_id="REQ-101", seen_hash=fake.card.content_hash,
                                    seen_sections=None, reason="ต้องแก้ไขเพิ่มเติม", key="changes_fake_0000001")
            self.assertEqual(fake.calls[-2:], ["comment:changes_requested", "status:Changes requested"])

    def test_no_github_graphql_outside_the_adapter(self):
        pattern = re.compile(r"updateProjectV2ItemFieldValue|addComment\(|projectV2|graphql", re.IGNORECASE)
        offenders = []
        for path in [Path("main.py"), *Path("services").glob("*.py"), *Path("static").glob("*.js"), *Path("templates").glob("*.html")]:
            if path.name != "review_tracker.py" and pattern.search(path.read_text(encoding="utf-8")):
                offenders.append(str(path))
        self.assertEqual(offenders, [])


class _TagCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags: list[tuple[str, list[tuple[str, str | None]]]] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, attrs))


XSS_VECTORS = [
    "<script>alert(1)</script>",
    "<SCRIPT SRC=//evil.example/x.js></SCRIPT>",
    '<img src=x onerror="alert(1)">',
    "<svg onload=alert(1)><circle/></svg>",
    "[click](javascript:alert(1))",
    "[click](JaVaScRiPt:alert(1))",
    "[click](java\tscript:alert(1))",
    "[data](data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==)",
    "![pic](https://evil.example/track.png)",
    '<a href="javascript:alert(1)">x</a>',
    "<iframe src=https://evil.example></iframe>",
    "<style>body{display:none}</style>",
    '<div onmouseover="alert(1)">hover</div>',
    '[x](https://ok.example/"onmouseover="alert(1))',
    "<form action=https://evil.example><input name=p></form>",
    "**bold <b onclick=alert(1)>x</b>**",
    "`<script>alert(1)</script>`",
    "```\n<script>alert(1)</script>\n```",
    "<!-- <script>alert(1)</script> -->",
    "| a | b |\n|---|---|\n| <img src=x onerror=alert(1)> | [x](javascript:1) |",
]


class SafeMarkdownTests(unittest.TestCase):
    def assertSafe(self, output: str):
        parser = _TagCollector()
        parser.feed(output)
        for tag, attrs in parser.tags:
            self.assertIn(tag, ALLOWED_TAGS, output)
            for name, value in attrs:
                self.assertIn(name, ALLOWED_ATTRS.get(tag, set()), output)
                if name == "href":
                    self.assertTrue(value.startswith(("https://", "mailto:")), output)
        lowered = output.lower()
        for bad in ("<script", "<img", "<svg", "<iframe", "<style", "<form", "javascript:", "href=\"data:"):
            self.assertNotIn(bad, lowered)
        self.assertIsNone(re.search(r"<[^>]+\son\w+=", lowered), output)

    def test_xss_vectors_are_neutralised(self):
        for vector in XSS_VECTORS:
            with self.subTest(vector=vector):
                self.assertSafe(render_markdown(vector).html)
                self.assertSafe(render_inline(vector).html)

    def test_sanitized_flag_is_raised_when_content_was_removed(self):
        for vector in ("<script>alert(1)</script> text", "[x](javascript:alert(1))", "![pic](https://e.example/p.png)"):
            self.assertTrue(render_markdown(vector).sanitized, vector)
        self.assertFalse(render_markdown("plain **text** with [link](https://github.com)").sanitized)

    def test_supported_markdown_and_link_policy(self):
        out = render_markdown("# Big\n\ntext [gh](https://github.com/x) `code`\n\n| a |\n|---|\n| 1 |\n\n> quote\n\n1. one\n2. two").html
        self.assertIn("<h3>Big</h3>", out)
        self.assertIn('<a href="https://github.com/x" rel="noopener noreferrer" target="_blank">gh</a>', out)
        self.assertIn('<div class="md-scroll" tabindex="0" role="region" aria-label="ตาราง"><table>', out)
        self.assertIn("<blockquote>", out)
        self.assertIn("<ol><li>one</li><li>two</li></ol>", out)
        self.assertIn("[รูป: pic] ↗", render_markdown("![pic](https://e.example/p.png)").html)
        self.assertIn("&lt;script&gt;", render_markdown("`<script>`").html)


class ParserTests(unittest.TestCase):
    def test_blocks_hash_and_criteria(self):
        items = parse_requirements(REQUIREMENTS, "demo")
        self.assertEqual([item.req_id for item in items], ["REQ-101", "REQ-102"])
        self.assertEqual(items[0].acceptance_criteria, ["ข้อแรก", "ข้อสอง"])
        self.assertEqual(items[0].group, "กลุ่ม A")
        self.assertEqual((items[0].scope_in, items[0].scope_out), (["หน้า review"], ["Jira adapter"]))
        self.assertNotIn("not part of a REQ", items[1].raw_block)
        same = parse_requirements(REQUIREMENTS.replace("\n", "\r\n"), "demo")
        self.assertEqual(items[0].content_hash, same[0].content_hash)
        changed = parse_requirements(REQUIREMENTS.replace("ข้อสอง", "ข้อสาม"), "demo")
        self.assertNotEqual(items[0].content_hash, changed[0].content_hash)
        self.assertEqual(items[1].content_hash, changed[1].content_hash)

    def test_real_app_dashboard_requirements_parse_when_available(self):
        path = Path("C:/Users/thedu/code/veda/.veda/projects/app-dashboard/requirements.md")
        if not path.exists():
            self.skipTest("veda repo not present")
        items = parse_requirements(path.read_text(encoding="utf-8"), "app-dashboard")
        self.assertEqual([item.req_id for item in items], [f"REQ-{n:03d}" for n in range(1, 19)])
        self.assertTrue(all(item.acceptance_criteria for item in items))


class DashboardNavigationTests(unittest.TestCase):
    def test_dashboard_links_review_page_for_reviewers_only(self):
        source = Path("templates/index.html").read_text(encoding="utf-8")
        self.assertIn('<a id="reviewLink" class="hidden" href="/review">Requirement review</a>', source)
        self.assertIn('fetch("/api/review/summary")', source)


if __name__ == "__main__":
    unittest.main()
