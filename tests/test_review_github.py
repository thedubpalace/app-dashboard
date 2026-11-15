"""GitHub adapter, one-way sync, requirements index and registry writer (REQ-001/002/003/004/012/016).

Everything runs against an in-memory fake GraphQL API (tests/fake_github.py)
or a local HTTP server on 127.0.0.1. No real network is used.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_github import FakeGitHub  # noqa: E402

from services import registry_writer, requirements_index  # noqa: E402
from services.github_projects import (  # noqa: E402
    GitHubConfig, GitHubProjectTracker, GraphQLClient, build_projects, decode_review_marker, load_token, urllib_transport,
)
from services.review_service import ReviewConfig, ReviewError, ReviewService  # noqa: E402
from services.review_sync import SyncAbort, SyncPermissionError, Syncer, bootstrap, guard_status, update_index_offline  # noqa: E402
from services.review_tracker import APPROVED, CHANGES_REQUESTED, DRAFT, NEEDS_REVIEW, TrackerError  # noqa: E402

TOKEN = "ghp_FAKEtokenFORtestsONLY0123456789abcd"
REQS = """# Demo

## Requirements

**REQ-101 First requirement**
- ทำสิ่งแรก
- AC: (a) ข้อแรก (b) ข้อสอง

**REQ-102 Second requirement**
- ทำสิ่งที่สอง
- AC: (a) ข้อเดียว
"""
REGISTRY = """{
  "$comment": "ทะเบียนโปรเจกต์ — keep formatting",
  "projects": [
    {
      "name": "other",
      "requirementsReady": true,
      "requirementsApproval": "legacy",
      "status":   "running"
    },
    {
      "name": "demo",
      "description": "ไทย text stays",
      "requirementsReady": true,
      "requirementsApproval": "legacy",
      "status": "running"
    }
  ],
  "controlResources": [ {"resourceId": "x", "n": 1.50} ],
  "updatedAt": "2026-11-14"
}
"""


class Env:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.veda = root / ".veda"
        self.projects = self.veda / "projects"
        (self.projects / "demo").mkdir(parents=True)
        (self.projects / "demo" / "requirements.md").write_text(REQS, encoding="utf-8")
        self.registry = self.projects / "registry.json"
        self.registry.write_text(REGISTRY, encoding="utf-8", newline="\n")
        self.index = self.veda / "requirements-index.json"
        self.env_file = root / "env.runtime"
        self.env_file.write_text(f"REVIEW_GITHUB_TOKEN={TOKEN}\n", encoding="utf-8")
        self.sync_log = self.veda / "audit" / "requirement-sync.jsonl"
        self.audit = self.veda / "audit" / "requirement-reviews.jsonl"
        self.runtime = root / "runtime"
        self.fake = FakeGitHub()
        self.gh = GitHubConfig(owner="thedubpalace", owner_type="user", project_number=1, repo="thedubpalace/requirements",
                               env_file=self.env_file, api_url="https://api.github.invalid/graphql", cache_seconds=0)

    def ops(self):
        return build_projects(self.gh, transport=self.fake)

    def syncer(self):
        return Syncer(self.ops(), projects_dir=self.projects, index_path=self.index, log_path=self.sync_log)

    def service(self, *, registry_write=True):
        config = ReviewConfig(mode="github", allowlist=["thedubpalace"], required_approvals=1, runtime_dir=self.runtime,
                              audit_path=self.audit, projects_dir=self.projects, mock_projects=[], registry_path=self.registry,
                              index_path=self.index, env_file=self.env_file, registry_write=registry_write, github=self.gh)
        return ReviewService(config, GitHubProjectTracker(self.gh, self.projects, self.index, transport=self.fake))

    def registry_demo(self):
        return registry_writer.read_project(self.registry, "demo")

    def edit_requirement(self, old, new):
        path = self.projects / "demo" / "requirements.md"
        path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")

    def cleanup(self):
        self.tmp.cleanup()


def approve(service, req_id, key):
    card = service.tracker.get(req_id)
    return service.approve(actor="thedubpalace", role="admin", session_id="sess", req_id=req_id, seen_hash=card.content_hash,
                           seen_sections=card.section_hashes, key=key)


# --------------------------------------------------------------- registry ----

class RegistryWriterTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.cleanup()

    def test_targeted_edit_changes_only_the_two_fields_of_one_project(self):
        approval = {"by": ["thedubpalace"], "at": "2026-11-15T00:00:00Z", "reqIds": ["REQ-101"], "hashes": {"REQ-101": "abc"}}
        changed = registry_writer.update_project_fields(self.env.registry, "demo", {"requirementsReady": False, "requirementsApproval": approval})
        self.assertTrue(changed)
        text = self.env.registry.read_text(encoding="utf-8")
        before, after = REGISTRY.split('"name": "demo"')
        self.assertTrue(text.startswith(before))  # everything before the project is byte-identical (incl. odd spacing)
        self.assertIn('"status":   "running"', text)
        self.assertTrue(text.endswith('  "controlResources": [ {"resourceId": "x", "n": 1.50} ],\n  "updatedAt": "2026-11-14"\n}\n'))
        data = json.loads(text)
        self.assertEqual(data["projects"][1]["requirementsApproval"], approval)
        self.assertIs(data["projects"][1]["requirementsReady"], False)
        self.assertEqual(data["projects"][0]["requirementsApproval"], "legacy")

    def test_idempotent_no_write_when_values_match(self):
        self.assertFalse(registry_writer.update_project_fields(self.env.registry, "demo", {"requirementsReady": True, "requirementsApproval": "legacy"}))
        self.assertEqual(self.env.registry.read_text(encoding="utf-8"), REGISTRY)

    def test_missing_field_is_inserted_and_other_fields_refused(self):
        text = REGISTRY.replace('      "requirementsApproval": "legacy",\n      "status": "running"', '      "status": "running"')
        self.env.registry.write_text(text, encoding="utf-8", newline="\n")
        registry_writer.update_project_fields(self.env.registry, "demo", {"requirementsReady": False, "requirementsApproval": None})
        data = json.loads(self.env.registry.read_text(encoding="utf-8"))
        self.assertIsNone(data["projects"][1]["requirementsApproval"])
        with self.assertRaises(registry_writer.RegistryWriteError):
            registry_writer.update_project_fields(self.env.registry, "demo", {"status": "stopped"})
        with self.assertRaises(registry_writer.RegistryWriteError):
            registry_writer.update_project_fields(self.env.registry, "missing", {"requirementsReady": False})

    def test_crlf_and_bom_are_preserved(self):
        self.env.registry.write_bytes(b"\xef\xbb\xbf" + REGISTRY.replace("\n", "\r\n").encode("utf-8"))
        registry_writer.update_project_fields(self.env.registry, "demo", {"requirementsApproval": {"by": ["a"], "at": "t", "reqIds": [], "hashes": {}}})
        raw = self.env.registry.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))

    def test_concurrent_writers_do_not_corrupt(self):
        errors = []

        def worker(flag):
            try:
                for _ in range(10):
                    registry_writer.update_project_fields(self.env.registry, "demo", {"requirementsReady": flag})
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)
        threads = [threading.Thread(target=worker, args=(i % 2 == 0,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        json.loads(self.env.registry.read_text(encoding="utf-8"))
        self.assertFalse(self.env.registry.with_name("registry.json.lock").exists())


# ------------------------------------------------------------------ index ----

class RequirementsIndexTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.cleanup()

    def test_concurrent_allocation_never_duplicates(self):
        results: list[str] = []
        lock = threading.Lock()

        def worker():
            ids = requirements_index.allocate(self.env.index, "demo", 3, reserved=["REQ-102"])
            with lock:
                results.extend(ids)
        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(results), 18)
        self.assertEqual(len(set(results)), 18)
        self.assertEqual(min(results), "REQ-103")

    def test_ids_are_stable_and_never_move_project(self):
        update_index_offline(self.env.projects, self.env.index, "demo")
        self.env.edit_requirement("First requirement", "First requirement renamed")
        update_index_offline(self.env.projects, self.env.index, "demo")
        data = requirements_index.load(self.env.index)
        self.assertEqual(data["requirements"]["REQ-101"]["title"], "First requirement renamed")
        self.assertEqual(data["nextNumber"], 103)
        with self.assertRaises(requirements_index.IndexError_):
            requirements_index.record(self.env.index, {"REQ-101": {"project": "other"}})
        (self.env.projects / "other").mkdir()
        (self.env.projects / "other" / "requirements.md").write_text("**REQ-101 Clash**\n- AC: (a) x\n", encoding="utf-8")
        with self.assertRaises(SyncAbort):
            update_index_offline(self.env.projects, self.env.index, "other")

    def test_removed_requirement_is_marked_inactive_not_deleted(self):
        update_index_offline(self.env.projects, self.env.index, "demo")
        path = self.env.projects / "demo" / "requirements.md"
        path.write_text(REQS.split("**REQ-102")[0], encoding="utf-8")
        update_index_offline(self.env.projects, self.env.index, "demo")
        entry = requirements_index.load(self.env.index)["requirements"]["REQ-102"]
        self.assertFalse(entry["active"])
        self.assertEqual(requirements_index.allocate(self.env.index, "demo", 1), ["REQ-103"])


# ------------------------------------------------------- client / token -----

class ClientAndTokenTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.cleanup()

    def test_token_is_read_from_the_env_file_only(self):
        with patch.dict(os.environ, {"REVIEW_GITHUB_TOKEN": "ghp_fromprocessenv"}):
            self.assertEqual(load_token(self.env.env_file), TOKEN)
            missing = Path(self.env.tmp.name) / "nope"
            self.assertIsNone(load_token(missing))
        self.env.env_file.write_text("OTHER=1\nREVIEW_GITHUB_TOKEN=\"ghp_quoted\"\n", encoding="utf-8")
        self.assertEqual(load_token(self.env.env_file), "ghp_quoted")

    def test_missing_token_config_and_fine_grained_token_fail_closed(self):
        self.env.env_file.write_text("", encoding="utf-8")
        with self.assertRaises(TrackerError) as ctx:
            build_projects(self.env.gh)
        self.assertEqual(ctx.exception.code, "token_missing")
        self.env.env_file.write_text("REVIEW_GITHUB_TOKEN=github_pat_11AAfinegrained\n", encoding="utf-8")
        with self.assertRaises(TrackerError) as ctx:
            build_projects(self.env.gh)
        self.assertEqual(ctx.exception.code, "token_scope")
        self.env.env_file.write_text(f"REVIEW_GITHUB_TOKEN={TOKEN}\n", encoding="utf-8")
        self.env.gh.project_number = None
        with self.assertRaises(TrackerError) as ctx:
            build_projects(self.env.gh)
        self.assertEqual(ctx.exception.code, "not_configured")

    def test_error_mapping_is_sanitized(self):
        cases = [
            (("http", 401), "token_expired"),
            (("http", 403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1900000000"}), "rate_limited"),
            (("http", 429, {"retry-after": "30"}), "rate_limited"),
            (("http", 403), "token_scope"),
            (("http", 502), "github_unavailable"),
            (("gql", "RATE_LIMITED"), "rate_limited"),
            (("gql", "INSUFFICIENT_SCOPES"), "token_scope"),
            (("gql", "NOT_FOUND"), "token_scope"),
            (("gql", "SOMETHING"), "github_unavailable"),
        ]
        for fail, code in cases:
            fake = FakeGitHub()
            fake.fail["*"] = fail
            client = GraphQLClient(TOKEN, transport=fake)
            with self.assertRaises(TrackerError) as ctx:
                client.execute("query ReviewViewer { viewer { login } }")
            self.assertEqual(ctx.exception.code, code, fail)
            text = f"{ctx.exception.message} {ctx.exception.details} {ctx.exception!r}"
            self.assertNotIn(TOKEN, text)
            if code == "rate_limited":
                self.assertTrue(ctx.exception.retry_at)
        self.assertEqual(fake.tokens_seen[-1], f"bearer {TOKEN}")

    def test_transport_failure_maps_to_unavailable(self):
        def broken(*_):
            from services.github_projects import TransportFailure
            raise TransportFailure(timeout=True)
        with self.assertRaises(TrackerError) as ctx:
            GraphQLClient(TOKEN, transport=broken).execute("query ReviewViewer { viewer { login } }")
        self.assertEqual((ctx.exception.code, ctx.exception.status), ("github_unavailable", 504))

    def test_urllib_transport_against_local_http_server(self):
        seen = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                seen["auth"] = self.headers.get("Authorization")
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen["query"] = body["query"]
                status = 401 if "Fail" in body["query"] else 200
                data = json.dumps({"data": {"viewer": {"login": "thedubpalace"}}}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("X-OAuth-Scopes", "project, repo")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = GraphQLClient(TOKEN, api_url=f"http://127.0.0.1:{server.server_address[1]}/graphql", transport=urllib_transport)
            self.assertEqual(client.execute("query ReviewViewer { viewer { login } }")["viewer"]["login"], "thedubpalace")
            self.assertEqual(seen["auth"], f"bearer {TOKEN}")
            self.assertEqual(client.last_headers.get("x-oauth-scopes"), "project, repo")
            with self.assertRaises(TrackerError) as ctx:
                client.execute("query ReviewFail { viewer { login } }")
            self.assertEqual(ctx.exception.code, "token_expired")
        finally:
            server.shutdown()
            server.server_close()

    def test_urllib_transport_refuses_plain_http_to_remote_hosts(self):
        from services.github_projects import TransportFailure
        with self.assertRaises(TransportFailure):
            urllib_transport("http://example.com/graphql", b"{}", {}, 1)


# ------------------------------------------------------- tracker / health ---

class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.env.syncer().run("demo")

    def tearDown(self):
        self.env.cleanup()

    def tracker(self):
        return GitHubProjectTracker(self.env.gh, self.env.projects, self.env.index, transport=self.env.fake)

    def test_cards_come_from_github_status_and_local_content(self):
        cards = self.tracker().list_pending()
        self.assertEqual([card.req_id for card in cards], ["REQ-101", "REQ-102"])
        card = cards[0]
        self.assertEqual(card.status, NEEDS_REVIEW)
        self.assertFalse(card.issue_is_mock)
        self.assertTrue(card.issue_url.startswith("https://github.com/thedubpalace/requirements/issues/"))
        self.assertEqual(card.acceptance_criteria, ["ข้อแรก", "ข้อสอง"])
        self.assertFalse(card.sync_pending)

    def test_unknown_status_draft_items_foreign_repo_and_archived_are_ignored(self):
        fake = self.env.fake
        fake.add_raw_item(req_id="REQ-101", project="demo", status="Todo")
        fake.add_raw_item(req_id="REQ-150", project="demo", status=NEEDS_REVIEW, typename="DraftIssue")
        fake.add_raw_item(req_id="REQ-151", project="demo", status=NEEDS_REVIEW, repo="someone/else")
        fake.add_raw_item(req_id="REQ-152", project="demo", status=NEEDS_REVIEW, archived=True)
        fake.add_raw_item(req_id="REQ-153", project="../etc", status=NEEDS_REVIEW)
        fake.set_status("REQ-102", "In progress")
        self.assertEqual([card.req_id for card in self.tracker().list_cards()], ["REQ-101"])

    def test_local_edit_without_sync_blocks_approve_and_unapproves(self):
        service = self.env.service()
        approve(service, "REQ-101", "approve_key_000000001")
        self.env.edit_requirement("ทำสิ่งแรก", "ทำสิ่งแรก (แก้)")
        card = service.tracker.get("REQ-101")
        self.assertEqual(card.status, DRAFT)  # Approved but content changed -> not approved (REQ-004 b)
        self.assertTrue(card.sync_pending)
        service.tracker.get("REQ-102")
        other = service.tracker.get("REQ-102")
        self.assertFalse(other.sync_pending)
        self.env.edit_requirement("ทำสิ่งที่สอง", "ทำสิ่งที่สอง (แก้)")
        with self.assertRaises(ReviewError) as ctx:
            approve(service, "REQ-102", "approve_key_000000002")
        self.assertEqual(ctx.exception.code, "card_incomplete")
        self.assertEqual(self.env.fake.status_of("REQ-102"), NEEDS_REVIEW)

    def test_health_scopes_expiry_and_project_setup(self):
        self.assertEqual(self.tracker().health().warnings, [])
        soon = (datetime.now(timezone.utc) + timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S UTC")
        self.env.fake.expiration = soon
        warning = self.tracker().health().warnings[0]
        self.assertEqual((warning["code"], warning["days"] in (4, 5)), ("token_expiring", True))
        self.env.fake.expiration = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S UTC")
        with self.assertRaises(TrackerError) as ctx:
            self.tracker().health()
        self.assertEqual(ctx.exception.code, "token_expired")
        self.env.fake.expiration = None
        self.env.fake.scopes = "repo"
        with self.assertRaises(TrackerError) as ctx:
            self.tracker().health()
        self.assertEqual((ctx.exception.code, ctx.exception.details["missingScopes"]), ("token_scope", ["project"]))
        self.env.fake.scopes = "project, repo"
        del self.env.fake.fields["Status"]["options"]["Changes requested"]
        with self.assertRaises(TrackerError) as ctx:
            self.tracker().health()
        self.assertEqual(ctx.exception.code, "not_configured")

    def test_review_path_cannot_set_sync_statuses(self):
        with self.assertRaises(ValueError):
            self.tracker().set_status("REQ-101", NEEDS_REVIEW, actor="x", content_hash="y")

    def test_decision_comments_from_other_authors_are_ignored(self):
        tracker = self.tracker()
        tracker.add_comment("REQ-101", "Changes requested", actor="thedubpalace", kind="changes_requested", content_hash="h", reason="ต้องแก้ข้อแรกให้ชัด")
        issue = self.env.fake.issue_for("REQ-101")
        forged = dict(issue["comments"][-1], id="IC_forged", author={"login": "mallory"})
        issue["comments"].append(forged)
        card = tracker.get("REQ-101")
        self.assertEqual([(c.actor, c.kind, c.reason) for c in card.comments], [("thedubpalace", "changes_requested", "ต้องแก้ข้อแรกให้ชัด")])
        self.assertEqual(decode_review_marker(issue["comments"][0]["body"])["reqId"], "REQ-101")


# ------------------------------------------------------------------- sync ----

class SyncTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.cleanup()

    def test_first_sync_creates_issue_card_fields_and_needs_review(self):
        report = self.env.syncer().run("demo")
        self.assertEqual([a.action for a in report.actions], ["create", "create"])
        fake = self.env.fake
        self.assertEqual(fake.status_of("REQ-101"), NEEDS_REVIEW)
        issue = fake.issue_for("REQ-101")
        self.assertTrue(issue["title"].startswith("REQ-101: First requirement"))
        self.assertIn("Do not edit here", issue["body"])
        self.assertEqual(fake.item_for("REQ-101")["fields"]["App"]["value"], "demo")
        entry = requirements_index.load(self.env.index)["requirements"]["REQ-101"]
        self.assertEqual((entry["project"], entry["issueNumber"], entry["active"]), ("demo", issue["number"], True))
        self.assertTrue(entry["itemId"].startswith("PVTI_"))

    def test_second_sync_is_idempotent(self):
        self.env.syncer().run("demo")
        before_index = self.env.index.read_text(encoding="utf-8")
        before_log = self.env.sync_log.read_text(encoding="utf-8")
        calls = len(self.env.fake.mutations())
        report = self.env.syncer().run("demo")
        self.assertEqual(report.writes, 0)
        self.assertEqual(len(self.env.fake.mutations()), calls)
        self.assertEqual(self.env.index.read_text(encoding="utf-8"), before_index)
        self.assertEqual(self.env.sync_log.read_text(encoding="utf-8"), before_log)

    def test_dry_run_writes_nothing(self):
        report = self.env.syncer().run("demo", dry_run=True)
        self.assertEqual([a.action for a in report.actions], ["create", "create"])
        self.assertEqual(self.env.fake.mutations(), [])
        self.assertFalse(self.env.index.exists())
        self.assertFalse(self.env.sync_log.exists())

    def test_content_change_after_approval_goes_back_to_review(self):
        self.env.syncer().run("demo")
        self.env.fake.set_status("REQ-101", APPROVED)
        self.env.edit_requirement("(b) ข้อสอง", "(b) ข้อสองแก้แล้ว")
        report = self.env.syncer().run("demo")
        action = report.actions[0]
        self.assertEqual((action.action, action.status_before, action.status_after), ("update", APPROVED, NEEDS_REVIEW))
        statuses = [v["value"]["singleSelectOptionId"] for name, v in self.env.fake.calls if name == "ReviewSetField" and "singleSelectOptionId" in v["value"]]
        self.assertEqual(statuses[-2:], ["O_0", "O_1"])  # Draft, then Needs review
        self.assertIn("ข้อสองแก้แล้ว", self.env.fake.issue_for("REQ-101")["body"])

    def test_changes_requested_card_stays_until_ba_edits(self):
        self.env.syncer().run("demo")
        self.env.fake.set_status("REQ-101", CHANGES_REQUESTED)
        self.assertEqual(self.env.syncer().run("demo").writes, 0)
        self.env.edit_requirement("ทำสิ่งแรก", "ทำสิ่งแรกฉบับแก้")
        self.env.syncer().run("demo")
        self.assertEqual(self.env.fake.status_of("REQ-101"), NEEDS_REVIEW)

    def test_issue_edited_on_github_is_overwritten_and_logged(self):
        self.env.syncer().run("demo")
        issue = self.env.fake.issue_for("REQ-102")
        original = issue["body"]
        issue["body"] = original.replace("ทำสิ่งที่สอง", "แก้บน GitHub")
        report = self.env.syncer().run("demo")
        self.assertEqual([a.action for a in report.actions], ["noop", "drift"])
        self.assertEqual(issue["body"], original)
        log = [json.loads(line) for line in self.env.sync_log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual((log[-1]["action"], log[-1]["reqId"]), ("drift", "REQ-102"))
        self.assertEqual(self.env.fake.status_of("REQ-102"), NEEDS_REVIEW)

    def test_sync_cannot_write_reviewer_statuses(self):
        for status in (APPROVED, CHANGES_REQUESTED):
            with self.assertRaises(SyncPermissionError):
                guard_status(status)
        syncer = self.env.syncer()
        with self.assertRaises(SyncPermissionError):
            syncer._set_status("PVTI_1", APPROVED, False)
        self.assertEqual(self.env.fake.mutations(), [])

    def test_sync_refuses_unbootstrapped_project_and_foreign_ids(self):
        fake = FakeGitHub(with_fields=False)
        self.env.fake = fake
        with self.assertRaises(SyncAbort):
            self.env.syncer().run("demo")
        result = bootstrap(self.env.ops(), dry_run=True)
        self.assertEqual(fake.mutations(), [])
        self.assertEqual(len(result["created"]), 2)
        result = bootstrap(self.env.ops(), dry_run=False)
        self.assertTrue(result["ok"], result)
        self.assertIn("REQ-ID", fake.fields)
        fake.add_raw_item(req_id="REQ-101", project="other", status=NEEDS_REVIEW)
        with self.assertRaises(SyncAbort):
            self.env.syncer().run("demo")

    def test_bootstrap_reports_missing_status_options_for_manual_fix(self):
        self.env.fake = FakeGitHub(status_options=["Todo", "In Progress", "Done"])
        result = bootstrap(self.env.ops(), dry_run=False)
        self.assertFalse(result["ok"])
        self.assertEqual(len(result["manual"]), 4)

    def test_crash_after_issue_creation_reuses_the_issue(self):
        self.env.fake.fail["ReviewAddItem"] = ("http", 502)
        with self.assertRaises(TrackerError):
            self.env.syncer().run("demo")
        self.assertEqual(len(self.env.fake.issues), 1)
        del self.env.fake.fail["ReviewAddItem"]
        self.env.syncer().run("demo")
        self.assertEqual(len(self.env.fake.issues), 2)
        self.assertEqual(len(self.env.fake.items), 2)


# ------------------------------------------------------ gate end-to-end -----

class GateEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.env.syncer().run("demo")

    def tearDown(self):
        self.env.cleanup()

    def test_full_review_round_trip_sets_and_clears_requirements_ready(self):
        service = self.env.service()
        queue = service.queue()  # reconcile on open: cards exist, none approved -> false
        self.assertEqual(len(queue["cards"]), 2)
        self.assertIs(self.env.registry_demo()["requirementsReady"], False)
        self.assertIsNone(self.env.registry_demo()["requirementsApproval"])

        # Request changes: comment first, then status (REQ-008)
        card = service.tracker.get("REQ-101")
        start = len(self.env.fake.calls)
        service.request_changes(actor="thedubpalace", role="admin", session_id="sess", req_id="REQ-101", seen_hash=card.content_hash,
                                seen_sections=None, reason="AC ข้อสองยังวัดผลไม่ได้", key="changes_key_000000001")
        names = [name for name, _ in self.env.fake.calls[start:] if name in ("ReviewComment", "ReviewSetField")]
        self.assertEqual(names, ["ReviewComment", "ReviewSetField"])
        self.assertEqual(self.env.fake.status_of("REQ-101"), CHANGES_REQUESTED)
        comment = self.env.fake.issue_for("REQ-101")["comments"][-1]["body"]
        self.assertIn("REQ-101", comment)
        self.assertIn(card.content_hash[:12], comment)

        # ba fixes, sync -> Needs review; approve both -> ready
        self.env.edit_requirement("(b) ข้อสอง", "(b) ข้อสองวัดได้")
        self.env.syncer().run("demo")
        approve(service, "REQ-101", "approve_key_000000001")
        self.assertIs(self.env.registry_demo()["requirementsReady"], False)
        result = approve(service, "REQ-102", "approve_key_000000002")
        self.assertTrue(result["isLastOfProject"])
        self.assertTrue(result["reconcile"]["applied"])
        demo = self.env.registry_demo()
        self.assertIs(demo["requirementsReady"], True)
        self.assertEqual(demo["requirementsApproval"]["by"], ["thedubpalace"])
        self.assertEqual(demo["requirementsApproval"]["reqIds"], ["REQ-101", "REQ-102"])
        self.assertIn("Approved by @thedubpalace", self.env.fake.issue_for("REQ-102")["comments"][-1]["body"])
        other = registry_writer.read_project(self.env.registry, "other")
        self.assertEqual((other["requirementsReady"], other["requirementsApproval"]), (True, "legacy"))  # grandfathered, untouched

        audit = [json.loads(line) for line in self.env.audit.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([(a["action"], a["result"]) for a in audit], [("request_changes", "succeeded"), ("approve", "succeeded"), ("approve", "succeeded")])
        self.assertTrue(all(a["issueUrl"].startswith("https://github.com/thedubpalace/requirements/issues/") for a in audit))
        self.assertNotIn(TOKEN, self.env.audit.read_text(encoding="utf-8"))

        # content changes after approval -> sync -> back to review and not ready (REQ-004 b)
        self.env.edit_requirement("ทำสิ่งที่สอง", "ทำสิ่งที่สองเพิ่มเติม")
        self.env.syncer().run("demo")
        service.queue()
        self.assertEqual(self.env.fake.status_of("REQ-102"), NEEDS_REVIEW)
        self.assertIs(self.env.registry_demo()["requirementsReady"], False)

    def test_reconcile_repairs_registry_after_crash(self):
        service = self.env.service()
        for req_id in ("REQ-101", "REQ-102"):
            self.env.fake.set_status(req_id, APPROVED)  # GitHub written, registry write never happened
        self.assertIs(self.env.registry_demo()["requirementsReady"], True)
        registry_writer.update_project_fields(self.env.registry, "demo", {"requirementsReady": False})
        service.queue()
        self.assertIs(self.env.registry_demo()["requirementsReady"], True)
        before = self.env.registry.read_text(encoding="utf-8")
        service.queue()
        self.assertEqual(self.env.registry.read_text(encoding="utf-8"), before)  # idempotent

    def test_github_failure_on_approve_changes_nothing(self):
        service = self.env.service()
        service.queue()
        self.env.fake.fail["ReviewSetField"] = ("http", 502)
        with self.assertRaises(TrackerError):
            approve(service, "REQ-101", "approve_key_000000009")
        self.assertEqual(self.env.fake.status_of("REQ-101"), NEEDS_REVIEW)
        self.assertIs(self.env.registry_demo()["requirementsReady"], False)

    def test_comment_failure_on_request_changes_keeps_status(self):
        service = self.env.service()
        self.env.fake.fail["ReviewComment"] = ("http", 502)
        card = service.tracker.get("REQ-101")
        with self.assertRaises(TrackerError):
            service.request_changes(actor="thedubpalace", role="admin", session_id="s", req_id="REQ-101", seen_hash=card.content_hash,
                                    seen_sections=None, reason="เหตุผลยาวพอสมควร", key="changes_key_000000009")
        self.assertEqual(self.env.fake.status_of("REQ-101"), NEEDS_REVIEW)

    def test_double_approve_is_idempotent(self):
        service = self.env.service()
        first = approve(service, "REQ-101", "approve_key_000000011")
        card_hash = service.tracker.get("REQ-101").content_hash
        second = service.approve(actor="thedubpalace", role="admin", session_id="s2", req_id="REQ-101", seen_hash=card_hash,
                                 seen_sections=None, key="approve_key_000000012")
        self.assertEqual((first["status"], second.get("alreadyApproved")), (APPROVED, True))
        approvals = [c for c in self.env.fake.issue_for("REQ-101")["comments"] if "Approved by" in c["body"]]
        self.assertEqual(len(approvals), 1)

    def test_registry_write_can_be_disabled(self):
        service = self.env.service(registry_write=False)
        result = service.queue()["gate"]["demo"]
        self.assertFalse(result["applied"])
        self.assertIs(self.env.registry_demo()["requirementsReady"], True)


# --------------------------------------------------------- HTTP surface -----

class GitHubModeApiTests(unittest.TestCase):
    """The real FastAPI app in github mode, with the fake injected as the HTTP transport."""

    def setUp(self):
        import main  # noqa: F401
        from services import review_api
        from test_review_queue import call
        self.call = call
        self.review_api = review_api
        self.env = Env()
        self.env.syncer().run("demo")
        env = {"VEDA_DASHBOARD_SESSION_SECRET": "session-secret", "VEDA_DASHBOARD_USERNAME": "thedubpalace", "VEDA_DASHBOARD_PASSWORD": "pw",
               "REVIEW_TRACKER": "github", "REVIEW_PROJECTS_DIR": str(self.env.projects), "REVIEW_RUNTIME_DIR": str(self.env.runtime),
               "REVIEW_ENV_FILE": str(self.env.env_file), "REVIEW_INDEX_PATH": str(self.env.index), "REVIEW_REGISTRY_PATH": str(self.env.registry),
               "REVIEW_AUDIT_PATH": str(self.env.audit), "REVIEW_GITHUB_PROJECT_NUMBER": "1", "REVIEW_GITHUB_CACHE_SECONDS": "0",
               "DASHBOARD_SECURITY_LOG": str(self.env.runtime / "security.log"), "REVIEW_GITHUB_TOKEN": "ghp_processenvmustbeignored"}
        self.patches = [patch.dict(os.environ, env), patch("services.github_projects.urllib_transport", self.env.fake)]
        for item in self.patches:
            item.start()
        for name in ("DASHBOARD_USER_ROLES", "REVIEW_ALLOWLIST", "REVIEW_REQUIRED_APPROVALS", "REVIEW_GITHUB_OWNER_TYPE", "REVIEW_REGISTRY_WRITE"):
            os.environ.pop(name, None)
        review_api._services.clear()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.review_api._services.clear()
        self.env.cleanup()

    def login(self):
        from services.dashboard_auth import create_session
        return create_session("thedubpalace")

    def test_queue_and_approve_through_the_api(self):
        cookie, csrf = self.login()
        status, data, _, raw = self.call("GET", "/api/review/queue", cookie=cookie)
        self.assertEqual((status, data["mode"], len(data["cards"])), (200, "github", 2), data)
        card = data["cards"][0]
        self.assertFalse(card["issue"]["mock"])
        self.assertTrue(card["issue"]["url"].startswith("https://github.com/thedubpalace/requirements/issues/"))
        self.assertNotIn(TOKEN.encode(), raw)
        status, result, _, raw = self.call("POST", f"/api/review/cards/{card['reqId']}/approve", cookie=cookie,
                                           headers={"x-csrf-token": csrf, "idempotency-key": "approve_api_00000001"},
                                           body={"hash": card["hash"], "sectionHashes": card["sectionHashes"]})
        self.assertEqual((status, result["status"]), (200, APPROVED), result)
        self.assertNotIn(TOKEN.encode(), raw)
        self.assertEqual(self.env.fake.status_of(card["reqId"]), APPROVED)
        self.assertTrue(all(auth == f"bearer {TOKEN}" for auth in self.env.fake.tokens_seen))

    def test_debug_endpoints_are_unavailable_in_github_mode(self):
        cookie, csrf = self.login()
        status, _, _, _ = self.call("POST", "/api/review/debug", cookie=cookie, headers={"x-csrf-token": csrf}, body={"action": "reset"})
        self.assertEqual(status, 404)

    def test_expired_token_shows_state_and_never_approves(self):
        cookie, csrf = self.login()
        self.env.fake.fail["*"] = ("http", 401)
        status, data, _, _ = self.call("GET", "/api/review/queue", cookie=cookie)
        self.assertEqual(data["code"], "token_expired")
        status, data, _, _ = self.call("POST", "/api/review/cards/REQ-101/approve", cookie=cookie,
                                       headers={"x-csrf-token": csrf, "idempotency-key": "approve_api_00000002"}, body={"hash": "x" * 64})
        self.assertEqual(data["code"], "token_expired")
        del self.env.fake.fail["*"]
        self.assertEqual(self.env.fake.status_of("REQ-101"), NEEDS_REVIEW)


# ------------------------------------------------------- generic naming ----

class GenericNamingTests(unittest.TestCase):
    """Nothing sent to GitHub carries the orchestrator's name; the app field name is configurable."""

    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.cleanup()

    def test_nothing_sent_to_github_mentions_veda(self):
        self.env.syncer().run("demo")
        bootstrap(self.env.ops(), dry_run=True)
        service = self.env.service()
        approve(service, "REQ-101", "approve_generic_0001")
        card = service.tracker.get("REQ-102")
        service.request_changes(actor="thedubpalace", role="admin", session_id="sess", req_id="REQ-102", seen_hash=card.content_hash,
                                seen_sections=card.section_hashes, reason="please clarify", key="changes_generic_0001")
        sent = self.env.fake.requests_seen
        self.assertTrue(sent)
        ops = {json.loads(body)["query"].split("(")[0].split("{")[0].split()[1] for body, _ in sent}
        self.assertTrue(ops and all(name.startswith("Review") for name in ops), ops)
        for body, headers in sent:
            self.assertNotIn("veda", body.decode("utf-8").lower())
            self.assertNotIn("veda", json.dumps(headers).lower())
            self.assertEqual(headers["User-Agent"], "requirement-review")
        issue = self.env.fake.issue_for("REQ-101")
        self.assertIn("**App:** `demo`", issue["body"])
        self.assertTrue(any("req-review:v1" in c["body"] for c in issue["comments"]))

    def test_app_field_name_is_configurable(self):
        self.env.fake = FakeGitHub(app_field="Service")
        self.env.gh.app_field = "Service"
        self.env.syncer().run("demo")
        self.assertEqual(self.env.fake.item_for("REQ-101")["fields"]["Service"]["value"], "demo")
        self.assertNotIn("App", self.env.fake.fields)
        cards = GitHubProjectTracker(self.env.gh, self.env.projects, self.env.index, transport=self.env.fake).list_pending()
        self.assertEqual([c.req_id for c in cards], ["REQ-101", "REQ-102"])

    def test_missing_app_field_is_reported_by_its_configured_name(self):
        self.env.fake = FakeGitHub(app_field="Service")
        result = bootstrap(self.env.ops(), dry_run=True)  # the default "App" field does not exist on this board
        self.assertIn("field 'App' (text) [dry-run]", result["created"])

    def test_invalid_app_field_name_is_a_config_problem(self):
        self.env.gh.app_field = 'App") { x'
        self.assertIn("REVIEW_GITHUB_APP_FIELD", self.env.gh.problems())
        with self.assertRaises(TrackerError):
            self.env.ops()

    def test_legacy_markers_are_still_read(self):
        from services.github_projects import body_hash, encode_review_marker
        self.assertEqual(body_hash("x\n<!-- veda-req:REQ-101@abc123 -->\n"), "abc123")
        self.assertEqual(body_hash("x\n<!-- req-sync:REQ-101@def456 -->\n"), "def456")
        legacy = encode_review_marker({"a": 1}).replace("req-review:v1", "veda-review:v1")
        self.assertEqual(decode_review_marker(legacy), {"a": 1})

    def test_env_names_defaults_and_projects_dir_fallback(self):
        registry = self.env.registry
        clean = {k: v for k, v in os.environ.items() if not k.startswith("REVIEW_")}
        with patch.dict(os.environ, clean, clear=True):
            config = ReviewConfig.from_env(registry)
            self.assertEqual((config.github.repo, config.github.app_field), ("thedubpalace/requirements", "App"))
            self.assertEqual(config.projects_dir, registry.parent)
            os.environ["REVIEW_VEDA_PROJECTS_DIR"] = str(self.env.runtime / "old")
            self.assertEqual(ReviewConfig.from_env(registry).projects_dir, self.env.runtime / "old")
            os.environ["REVIEW_PROJECTS_DIR"] = str(self.env.runtime / "new")
            os.environ["REVIEW_GITHUB_APP_FIELD"] = "Service"
            config = ReviewConfig.from_env(registry)
            self.assertEqual((config.projects_dir, config.github.app_field), (self.env.runtime / "new", "Service"))


if __name__ == "__main__":
    unittest.main()
