"""GitHub ProjectV2 + central issue repo adapter (REQ-001, REQ-016, REQ-017).

This is the only module that knows GitHub GraphQL, GitHub field names or
issue/item ids. Review logic uses `GitHubProjectTracker` through the
`RequirementTracker` interface; the one-way sync (`services/review_sync.py`)
uses the `GitHubProjects` operations below.

Token: read only from the dashboard's ignored `.env.runtime` file (key
`REVIEW_GITHUB_TOKEN`), never from the process environment, never logged,
never put in an error message or a response.

Token type (checked 2026-11-15 against docs.github.com): fine-grained PATs
cannot access Projects owned by a *user* account (listed under
"fine-grained personal access tokens limitations"; account permissions have
no Projects entry). For the user-owned "Veda Requirements" project use a
classic PAT with scopes `project` + `repo` (private central repo), or move
the Project to an organization and use a fine-grained PAT / GitHub App.
"""
from __future__ import annotations

import base64
import json
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import socket
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from services import requirements_index
from services.review_parser import ParsedRequirement, parse_file
from services.review_tracker import (
    APPROVED, CHANGES_REQUESTED, DRAFT, NEEDS_REVIEW, REVIEWER_STATUSES, STATUSES, Card, RequirementTracker,
    ReviewComment, TrackerError, TrackerHealth, section_hashes,
)

API_URL = "https://api.github.com/graphql"
TOKEN_KEY = "REVIEW_GITHUB_TOKEN"
FIELD_STATUS = "Status"
FIELD_REQ_ID = "REQ-ID"
FIELD_PROJECT = "Veda Project"
REQ_ID = re.compile(r"^REQ-\d{3,}$")
MARKER_BODY = re.compile(r"<!--\s*veda-req:(\S+)\s*-->")
MARKER_REVIEW = re.compile(r"<!--\s*veda-review:v1\s+([A-Za-z0-9_=-]+)\s*-->")
SYNC_STATUSES = (DRAFT, NEEDS_REVIEW)


# ---------------------------------------------------------------- token ----

def load_token(env_file: Path) -> str | None:
    """Return `REVIEW_GITHUB_TOKEN` from the env file, or None. Never reads os.environ."""
    try:
        lines = env_file.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return None
    for line in lines:
        match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        if match and match.group(1) == TOKEN_KEY:
            value = match.group(2).strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            return value or None
    return None


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_expiration(header: str | None) -> datetime | None:
    """`github-authentication-token-expiration`, e.g. `2026-12-15 10:00:00 UTC` or `... +0000`."""
    if not header:
        return None
    text = header.strip().replace(" UTC", " +0000")
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


# --------------------------------------------------------------- client ----

# transport(url, body, headers, timeout) -> (status, lower-cased headers, body bytes)
Transport = Callable[[str, bytes, dict[str, str], float], tuple[int, dict[str, str], bytes]]


class TransportFailure(Exception):
    def __init__(self, timeout: bool = False):
        super().__init__("transport failure")
        self.timeout = timeout


def urllib_transport(url: str, body: bytes, headers: dict[str, str], timeout: float) -> tuple[int, dict[str, str], bytes]:
    if not url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
        raise TransportFailure()
    request = Request(url, data=body, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed scheme check above
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()
    except HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, exc.read() if exc.fp else b""
    except (socket.timeout, TimeoutError):
        raise TransportFailure(timeout=True) from None
    except (URLError, OSError):
        raise TransportFailure() from None


class GraphQLClient:
    """Thin GraphQL transport with sanitized error mapping (design R7/R14)."""

    def __init__(self, token: str, *, api_url: str = API_URL, timeout: float = 15.0,
                 transport: Transport | None = None, clock: Callable[[], datetime] | None = None):
        self._token = token
        self.api_url = api_url
        self.timeout = timeout
        self.transport = transport or urllib_transport
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.last_headers: dict[str, str] = {}

    def _rate_limited(self, headers: dict[str, str]) -> TrackerError:
        reset = headers.get("x-ratelimit-reset")
        retry_after = headers.get("retry-after")
        if reset and reset.isdigit():
            at = datetime.fromtimestamp(int(reset), tz=timezone.utc)
        elif retry_after and retry_after.isdigit():
            at = self.clock() + timedelta(seconds=int(retry_after))
        else:
            at = self.clock() + timedelta(minutes=1)
        return TrackerError("rate_limited", "GitHub จำกัดจำนวนคำขอ — ยังไม่ได้เปลี่ยนอะไร", status=429, retry_at=_iso(at))

    def execute(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {"Authorization": f"bearer {self._token}", "Accept": "application/vnd.github+json",
                   "Content-Type": "application/json", "User-Agent": "veda-dashboard-review"}
        payload = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
        try:
            status, response_headers, raw = self.transport(self.api_url, payload, headers, self.timeout)
        except TransportFailure as exc:
            if exc.timeout:
                raise TrackerError("github_unavailable", "GitHub ไม่ตอบภายในเวลาที่กำหนด", status=504) from None
            raise TrackerError("github_unavailable", "เชื่อมต่อ GitHub ไม่ได้", status=503) from None
        response_headers = {k.lower(): v for k, v in response_headers.items()}
        self.last_headers = {k: v for k, v in response_headers.items() if k.startswith(("x-ratelimit", "x-oauth", "x-accepted", "github-authentication"))}
        if status == 401:
            raise TrackerError("token_expired", "GitHub ปฏิเสธ token (ผิด หมดอายุ หรือถูกเพิกถอน)", status=503)
        if status in (403, 429):
            if response_headers.get("x-ratelimit-remaining") == "0" or response_headers.get("retry-after") or status == 429:
                raise self._rate_limited(response_headers)
            raise TrackerError("token_scope", "GitHub token สิทธิ์ไม่พอ", status=503)
        if status >= 500:
            raise TrackerError("github_unavailable", f"GitHub ตอบ HTTP {status}", status=503)
        if status != 200:
            raise TrackerError("github_unavailable", f"GitHub ตอบ HTTP {status}", status=502)
        try:
            body = json.loads(raw.decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            raise TrackerError("github_unavailable", "GitHub ตอบข้อมูลที่อ่านไม่ได้", status=502) from None
        errors = body.get("errors") or []
        if errors:
            types = {str(error.get("type") or "") for error in errors if isinstance(error, dict)}
            if "RATE_LIMITED" in types:
                raise self._rate_limited(response_headers)
            if "INSUFFICIENT_SCOPES" in types:
                raise TrackerError("token_scope", "GitHub token สิทธิ์ไม่พอ", status=503,
                                   details={"missingScopes": _missing_scopes(response_headers)})
            if types & {"NOT_FOUND", "FORBIDDEN"}:
                raise TrackerError("token_scope", "ไม่พบ Project/repo หรือ token เข้าถึงไม่ได้ (fine-grained PAT ใช้กับ Project ของ user ไม่ได้ — ใช้ classic PAT scope project + repo)",
                                   status=503, details={"missingScopes": _missing_scopes(response_headers)})
            raise TrackerError("github_unavailable", f"GitHub GraphQL error ({', '.join(sorted(t for t in types if t)) or 'unknown'})", status=502)
        data = body.get("data")
        if not isinstance(data, dict):
            raise TrackerError("github_unavailable", "GitHub ตอบข้อมูลไม่ครบ", status=502)
        return data


def _scopes(value: str | None) -> set[str]:
    return {item.strip() for item in (value or "").split(",") if item.strip()}


def _missing_scopes(headers: dict[str, str]) -> list[str]:
    granted = _scopes(headers.get("x-oauth-scopes"))
    missing = []
    if "project" not in granted:
        missing.append("project")
    if not granted & {"repo", "public_repo"}:
        missing.append("repo")
    return missing


# ------------------------------------------------------------- markers -----

def encode_review_marker(payload: dict[str, Any]) -> str:
    raw = base64.urlsafe_b64encode(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).decode("ascii")
    return f"<!-- veda-review:v1 {raw} -->"


def decode_review_marker(body: str) -> dict[str, Any] | None:
    match = MARKER_REVIEW.search(body or "")
    if not match:
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(match.group(1).encode("ascii")).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def body_hash(body: str | None) -> str | None:
    """Content hash recorded by the sync in the issue body marker (`veda-req:REQ-001@<sha256>`)."""
    match = MARKER_BODY.search(body or "")
    if not match or "@" not in match.group(1):
        return None
    return match.group(1).split("@", 1)[1]


def render_issue_body(item: ParsedRequirement) -> str:
    return (
        f"> Synced from `.veda/projects/{item.project}/requirements.md` by Veda. "
        "Do not edit here: changes are overwritten by the next sync and logged as drift.\n\n"
        f"**Veda project:** `{item.project}`  \n**Content hash:** `{item.content_hash[:12]}`\n\n"
        f"{item.raw_block.strip()}\n\n"
        f"<!-- veda-req:{item.req_id}@{item.content_hash} -->\n"
    )


def issue_title(item: ParsedRequirement) -> str:
    return f"{item.req_id}: {item.title}"[:250]


# ----------------------------------------------------------- operations ----

@dataclass
class ProjectMeta:
    project_id: str
    title: str
    url: str | None
    fields: dict[str, dict[str, Any]]  # name -> {id, dataType, options{name: id}}

    def missing(self) -> list[str]:
        problems = []
        status = self.fields.get(FIELD_STATUS)
        if not status or status.get("dataType") != "SINGLE_SELECT":
            problems.append(f"field '{FIELD_STATUS}' (single select)")
        else:
            for name in STATUSES:
                if name not in status["options"]:
                    problems.append(f"Status option '{name}'")
        for name in (FIELD_REQ_ID, FIELD_PROJECT):
            if name not in self.fields:
                problems.append(f"field '{name}'")
        return problems


@dataclass
class ProjectItem:
    item_id: str
    status: str | None
    status_updated_at: str | None
    req_id: str | None
    veda_project: str | None
    issue_id: str
    issue_number: int
    issue_url: str
    issue_title: str
    issue_body: str
    repository: str
    archived: bool
    comments: list[dict[str, Any]] = field(default_factory=list)


_FIELD_VALUE = """
  ... on ProjectV2ItemFieldSingleSelectValue { name updatedAt }
  ... on ProjectV2ItemFieldTextValue { text updatedAt }
"""


class GitHubProjects:
    """GraphQL operations for one ProjectV2 + one central repository."""

    def __init__(self, client: GraphQLClient, *, owner: str, owner_type: str, project_number: int, repo: str):
        if owner_type not in ("user", "organization"):
            raise ValueError("owner_type must be user or organization")
        self.client = client
        self.owner = owner
        self.owner_type = owner_type
        self.project_number = project_number
        self.repo = repo
        self._meta: ProjectMeta | None = None
        self._repo_id: str | None = None
        self._lock = threading.RLock()

    def _owner(self) -> str:
        return f"owner: {self.owner_type}(login: $login)"

    def viewer(self) -> dict[str, Any]:
        data = self.client.execute("query VedaViewer { viewer { login } rateLimit { remaining resetAt } }")
        return {"login": (data.get("viewer") or {}).get("login"), "rateLimit": data.get("rateLimit") or {},
                "headers": dict(self.client.last_headers)}

    def meta(self, *, refresh: bool = False) -> ProjectMeta:
        with self._lock:
            if self._meta and not refresh:
                return self._meta
            query = (
                "query VedaProjectMeta($login: String!, $number: Int!) { " + self._owner() + " { projectV2(number: $number) { id title url "
                "fields(first: 50) { nodes { __typename "
                "... on ProjectV2FieldCommon { id name dataType } "
                "... on ProjectV2SingleSelectField { options { id name } } } } } } }"
            )
            data = self.client.execute(query, {"login": self.owner, "number": self.project_number})
            project = ((data.get("owner") or {}).get("projectV2"))
            if not project:
                raise TrackerError("token_scope", "ไม่พบ Project หรือ token เข้าถึงไม่ได้", status=503)
            fields: dict[str, dict[str, Any]] = {}
            for node in (project.get("fields") or {}).get("nodes") or []:
                if not node or not node.get("name"):
                    continue
                fields[node["name"]] = {"id": node.get("id"), "dataType": node.get("dataType"),
                                        "options": {opt["name"]: opt["id"] for opt in node.get("options") or [] if opt}}
            self._meta = ProjectMeta(project["id"], project.get("title") or "", project.get("url"), fields)
            return self._meta

    def repository_id(self) -> str:
        with self._lock:
            if self._repo_id:
                return self._repo_id
            owner, name = self.repo.split("/", 1)
            data = self.client.execute("query VedaRepo($owner: String!, $name: String!) { repository(owner: $owner, name: $name) { id nameWithOwner isPrivate } }",
                                       {"owner": owner, "name": name})
            repo = data.get("repository")
            if not repo:
                raise TrackerError("token_scope", "ไม่พบ repo กลาง หรือ token เข้าถึงไม่ได้", status=503)
            self._repo_id = repo["id"]
            return self._repo_id

    def items(self) -> list[ProjectItem]:
        query = (
            "query VedaItems($login: String!, $number: Int!, $cursor: String) { " + self._owner() + " { projectV2(number: $number) { "
            "items(first: 100, after: $cursor) { pageInfo { hasNextPage endCursor } nodes { id isArchived "
            f"status: fieldValueByName(name: \"{FIELD_STATUS}\") {{ {_FIELD_VALUE} }} "
            f"reqId: fieldValueByName(name: \"{FIELD_REQ_ID}\") {{ {_FIELD_VALUE} }} "
            f"vedaProject: fieldValueByName(name: \"{FIELD_PROJECT}\") {{ {_FIELD_VALUE} }} "
            "content { __typename ... on Issue { id number url title body repository { nameWithOwner } "
            "comments(last: 50) { nodes { id body createdAt url author { login } } } } } } } } } }"
        )
        result: list[ProjectItem] = []
        cursor = None
        for _ in range(50):  # hard page cap: 5000 items
            data = self.client.execute(query, {"login": self.owner, "number": self.project_number, "cursor": cursor})
            project = (data.get("owner") or {}).get("projectV2")
            if not project:
                raise TrackerError("token_scope", "ไม่พบ Project หรือ token เข้าถึงไม่ได้", status=503)
            page = project.get("items") or {}
            for node in page.get("nodes") or []:
                content = (node or {}).get("content") or {}
                if content.get("__typename") != "Issue":
                    continue  # draft items / PRs are not requirement cards (REQ-001 b)
                status = node.get("status") or {}
                req = node.get("reqId") or {}
                project_value = node.get("vedaProject") or {}
                result.append(ProjectItem(
                    item_id=node["id"],
                    status=status.get("name"),
                    status_updated_at=status.get("updatedAt"),
                    req_id=(req.get("text") or req.get("name") or "").strip() or None,
                    veda_project=(project_value.get("text") or project_value.get("name") or "").strip() or None,
                    issue_id=content["id"],
                    issue_number=int(content["number"]),
                    issue_url=str(content.get("url") or ""),
                    issue_title=str(content.get("title") or ""),
                    issue_body=str(content.get("body") or ""),
                    repository=str((content.get("repository") or {}).get("nameWithOwner") or ""),
                    archived=bool(node.get("isArchived")),
                    comments=[c for c in (content.get("comments") or {}).get("nodes") or [] if c],
                ))
            info = page.get("pageInfo") or {}
            if not info.get("hasNextPage"):
                return result
            cursor = info.get("endCursor")
        raise TrackerError("github_unavailable", "Project มี item มากเกินไป", status=502)

    # ---------- mutations ----------
    def _set_field(self, item_id: str, field_name: str, value: dict[str, Any]) -> None:
        meta = self.meta()
        field_meta = meta.fields.get(field_name)
        if not field_meta:
            raise TrackerError("not_configured", f"Project ไม่มี field '{field_name}'", status=503)
        self.client.execute(
            "mutation VedaSetField($project: ID!, $item: ID!, $field: ID!, $value: ProjectV2FieldValue!) { "
            "updateProjectV2ItemFieldValue(input: {projectId: $project, itemId: $item, fieldId: $field, value: $value}) { projectV2Item { id } } }",
            {"project": meta.project_id, "item": item_id, "field": field_meta["id"], "value": value})

    def set_status_option(self, item_id: str, status: str) -> None:
        if status not in STATUSES:
            raise ValueError("unknown status")
        option = self.meta().fields.get(FIELD_STATUS, {}).get("options", {}).get(status)
        if not option:
            option = self.meta(refresh=True).fields.get(FIELD_STATUS, {}).get("options", {}).get(status)
        if not option:
            raise TrackerError("not_configured", f"Status field ไม่มีตัวเลือก '{status}'", status=503)
        self._set_field(item_id, FIELD_STATUS, {"singleSelectOptionId": option})

    def set_text(self, item_id: str, field_name: str, text: str) -> None:
        self._set_field(item_id, field_name, {"text": text})

    def add_comment(self, subject_id: str, body: str) -> str:
        data = self.client.execute(
            "mutation VedaComment($subject: ID!, $body: String!) { addComment(input: {subjectId: $subject, body: $body}) { commentEdge { node { id url } } } }",
            {"subject": subject_id, "body": body})
        node = (((data.get("addComment") or {}).get("commentEdge") or {}).get("node") or {})
        if not node.get("id"):
            raise TrackerError("github_unavailable", "GitHub ไม่คืน comment id", status=502)
        return str(node["id"])

    def create_issue(self, title: str, body: str) -> dict[str, Any]:
        data = self.client.execute(
            "mutation VedaCreateIssue($repo: ID!, $title: String!, $body: String!) { createIssue(input: {repositoryId: $repo, title: $title, body: $body}) { issue { id number url } } }",
            {"repo": self.repository_id(), "title": title, "body": body})
        issue = (data.get("createIssue") or {}).get("issue")
        if not issue:
            raise TrackerError("github_unavailable", "สร้าง issue ไม่สำเร็จ", status=502)
        return issue

    def update_issue(self, issue_id: str, title: str, body: str) -> None:
        self.client.execute(
            "mutation VedaUpdateIssue($id: ID!, $title: String!, $body: String!) { updateIssue(input: {id: $id, title: $title, body: $body}) { issue { id } } }",
            {"id": issue_id, "title": title, "body": body})

    def add_item(self, content_id: str) -> str:
        data = self.client.execute(
            "mutation VedaAddItem($project: ID!, $content: ID!) { addProjectV2ItemById(input: {projectId: $project, contentId: $content}) { item { id } } }",
            {"project": self.meta().project_id, "content": content_id})
        item = (data.get("addProjectV2ItemById") or {}).get("item")
        if not item:
            raise TrackerError("github_unavailable", "เพิ่ม card ใน Project ไม่สำเร็จ", status=502)
        return str(item["id"])

    def create_text_field(self, name: str) -> str:
        data = self.client.execute(
            "mutation VedaCreateField($project: ID!, $name: String!) { createProjectV2Field(input: {projectId: $project, dataType: TEXT, name: $name}) { "
            "projectV2Field { ... on ProjectV2FieldCommon { id name } } } }",
            {"project": self.meta().project_id, "name": name})
        created = (data.get("createProjectV2Field") or {}).get("projectV2Field") or {}
        self.meta(refresh=True)
        return str(created.get("id") or "")


# --------------------------------------------------------------- config ----

@dataclass
class GitHubConfig:
    owner: str
    owner_type: str
    project_number: int | None
    repo: str
    env_file: Path
    api_url: str = API_URL
    cache_seconds: float = 20.0
    expiry_warning_days: int = 14
    timeout: float = 15.0

    def problems(self) -> list[str]:
        problems = []
        if not self.project_number:
            problems.append("REVIEW_GITHUB_PROJECT_NUMBER")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repo or ""):
            problems.append("REVIEW_GITHUB_REPO")
        if not re.fullmatch(r"[A-Za-z0-9-]+", self.owner or ""):
            problems.append("REVIEW_GITHUB_OWNER")
        return problems


def build_projects(config: GitHubConfig, *, transport: Transport | None = None) -> GitHubProjects:
    """Validated operations object, or a sanitized TrackerError (missing token/config, wrong token type)."""
    token = load_token(config.env_file)
    if not token:
        raise TrackerError("token_missing", "ยังไม่ได้ตั้งค่า GitHub token (REVIEW_GITHUB_TOKEN ใน .env.runtime)")
    problems = config.problems()
    if problems:
        raise TrackerError("not_configured", f"ยังไม่ได้ตั้งค่า GitHub: {', '.join(problems)}")
    if config.owner_type == "user" and token.startswith("github_pat_"):
        raise TrackerError("token_scope", "fine-grained PAT เข้าถึง Project ของ user account ไม่ได้ — ใช้ classic PAT scope project + repo",
                           details={"missingScopes": ["classic PAT: project, repo"]})
    client = GraphQLClient(token, api_url=config.api_url, timeout=config.timeout, transport=transport)
    return GitHubProjects(client, owner=config.owner, owner_type=config.owner_type, project_number=int(config.project_number or 0), repo=config.repo)


# -------------------------------------------------------------- tracker ----

def _valid_project_name(name: str | None) -> bool:
    return bool(name) and bool(requirements_index.PROJECT_NAME.match(name or ""))


class GitHubProjectTracker(RequirementTracker):
    """`RequirementTracker` backed by ProjectV2 "Veda Requirements" + the central issue repo.

    Content comes from `.veda/projects/<name>/requirements.md` (source of
    truth); status and review comments come from GitHub. A card whose local
    content differs from what the sync last wrote to the issue is shown as
    "sync pending" and cannot be approved; an Approved card whose content
    changed counts as Draft (REQ-004 b) until the sync sends it back to review.
    """

    mode = "github"

    def __init__(self, config: GitHubConfig, projects_dir: Path, index_path: Path, *,
                 transport: Transport | None = None, clock: Callable[[], datetime] | None = None):
        self.config = config
        self.projects_dir = projects_dir
        self.index_path = index_path
        self.transport = transport
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._ops: GitHubProjects | None = None
        self._ops_error: TrackerError | None = None
        self._cache: tuple[float, list[Card], dict[str, ProjectItem]] | None = None
        self._viewer: tuple[float, dict[str, Any]] | None = None
        self._lock = threading.RLock()

    # ---------- plumbing ----------
    def ops(self) -> GitHubProjects:
        if self._ops is None:
            self._ops = build_projects(self.config, transport=self.transport)
        return self._ops

    def invalidate(self) -> None:
        with self._lock:
            self._cache = None

    def _viewer_info(self) -> dict[str, Any]:
        with self._lock:
            if self._viewer and time.monotonic() - self._viewer[0] < 300:
                return self._viewer[1]
            info = self.ops().viewer()
            if not info.get("login"):
                raise TrackerError("token_expired", "GitHub ไม่ยืนยันตัวตนของ token", status=503)
            self._viewer = (time.monotonic(), info)
            return info

    # ---------- contract ----------
    def health(self) -> TrackerHealth:
        info = self._viewer_info()
        headers = info.get("headers") or {}
        if "x-oauth-scopes" in headers:
            missing = _missing_scopes(headers)
            if missing:
                raise TrackerError("token_scope", "GitHub token สิทธิ์ไม่พอ", details={"missingScopes": missing})
        problems = self.ops().meta().missing()
        if problems:
            problems = self.ops().meta(refresh=True).missing()  # the owner may have fixed the fields meanwhile
        if problems:
            raise TrackerError("not_configured", f"Project ยังตั้งไม่ครบ: {', '.join(problems)} — รัน scripts/review_sync.py bootstrap")
        warnings: list[dict[str, Any]] = []
        expires = parse_expiration(headers.get("github-authentication-token-expiration"))
        if expires:
            days = (expires - self.clock()).total_seconds() / 86400
            if days <= 0:
                raise TrackerError("token_expired", "GitHub token หมดอายุแล้ว")
            if days <= self.config.expiry_warning_days:
                warnings.append({"code": "token_expiring", "days": max(1, int(days)), "expiresAt": _iso(expires)})
        return TrackerHealth(ok=True, token_expires_at=_iso(expires) if expires else None, warnings=warnings)

    def _local(self, project: str) -> dict[str, ParsedRequirement]:
        path = self.projects_dir / project / "requirements.md"
        if not path.is_file():
            return {}
        result: dict[str, ParsedRequirement] = {}
        for item in parse_file(path, project):
            result.setdefault(item.req_id, item)
        return result

    def _index(self) -> dict[str, Any]:
        try:
            return requirements_index.load(self.index_path)
        except requirements_index.IndexError_:
            return requirements_index.empty()

    def _card(self, item: ProjectItem, local: ParsedRequirement, viewer: str | None) -> Card:
        synced = body_hash(item.issue_body)
        sync_pending = synced != local.content_hash
        status = item.status or DRAFT
        if status == APPROVED and sync_pending:
            status = DRAFT  # content changed after approval: not approved any more (REQ-004 b)
        comments: list[ReviewComment] = []
        for node in item.comments:
            author = ((node.get("author") or {}).get("login") or "")
            marker = decode_review_marker(node.get("body") or "")
            if not marker or (viewer and author.lower() != viewer.lower()):
                continue  # only decision comments written by the dashboard's own token count
            comments.append(ReviewComment(id=str(node.get("id")), at=str(marker.get("at") or node.get("createdAt") or ""),
                                          actor=str(marker.get("actor") or author), kind=str(marker.get("kind") or "note"),
                                          body="", content_hash=str(marker.get("hash") or ""), reason=marker.get("reason")))
        approvals: list[dict[str, str]] = []
        if status == APPROVED:
            approvals = [{"actor": c.actor, "at": c.at, "hash": c.content_hash} for c in comments
                         if c.kind == "approved" and c.content_hash == local.content_hash]
            if not approvals:
                approvals = [{"actor": "unknown", "at": item.status_updated_at or "", "hash": local.content_hash}]
        prefix = f"https://github.com/{self.config.repo}/issues/"
        issue_url = item.issue_url if item.issue_url.startswith(prefix) else None
        return Card(
            req_id=local.req_id, project=local.project, title=local.title, content_markdown=local.content_markdown,
            acceptance_criteria=local.acceptance_criteria, scope_in=local.scope_in, scope_out=local.scope_out,
            scope_source=local.scope_source, content_hash=local.content_hash, section_hashes=section_hashes(local),
            status=status, requested_at=item.status_updated_at, issue_number=item.issue_number, issue_url=issue_url,
            issue_is_mock=False, group=local.group, comments=comments, approvals=approvals, sync_pending=sync_pending,
        )

    def _load_cards(self) -> tuple[list[Card], dict[str, ProjectItem]]:
        viewer = self._viewer_info().get("login")
        items = self.ops().items()
        index = self._index()["requirements"]
        locals_by_project: dict[str, dict[str, ParsedRequirement]] = {}
        cards: list[Card] = []
        by_req: dict[str, ProjectItem] = {}
        for item in items:
            if item.archived or item.repository.lower() != self.config.repo.lower():
                continue
            if item.status is not None and item.status not in STATUSES:
                continue  # REQ-001 c: unknown Status values are ignored
            if not item.req_id or not REQ_ID.match(item.req_id) or not _valid_project_name(item.veda_project):
                continue
            entry = index.get(item.req_id) or {}
            if entry and (entry.get("active") is False or entry.get("project") != item.veda_project):
                continue  # withdrawn, or the card claims a project that does not own this id
            if item.req_id in by_req:
                continue  # duplicate card for one REQ-id: first wins
            project = str(item.veda_project)
            if project not in locals_by_project:
                locals_by_project[project] = self._local(project)
            local = locals_by_project[project].get(item.req_id)
            if not local:
                continue  # removed from requirements.md: not active
            by_req[item.req_id] = item
            cards.append(self._card(item, local, viewer))
        return cards, by_req

    def list_cards(self, project: str | None = None) -> list[Card]:
        with self._lock:
            if self._cache and time.monotonic() - self._cache[0] < min(60.0, self.config.cache_seconds):
                cards = self._cache[1]
            else:
                cards, by_req = self._load_cards()
                self._cache = (time.monotonic(), cards, by_req)
        return [card for card in cards if project is None or card.project == project]

    def get(self, req_id: str) -> Card | None:
        with self._lock:
            self.invalidate()  # decisions always re-fetch (REQ-007)
            cards = self.list_cards()
        return next((card for card in cards if card.req_id == req_id), None)

    def _item(self, req_id: str) -> ProjectItem:
        with self._lock:
            if not self._cache:
                self.list_cards()
            item = self._cache[2].get(req_id) if self._cache else None
        if not item:
            raise TrackerError("conflict_state", "ไม่พบ requirement นี้ใน GitHub Project", status=409)
        return item

    def set_status(self, req_id: str, status: str, *, actor: str, content_hash: str) -> None:
        if status not in REVIEWER_STATUSES:
            raise ValueError("the review path may only set Approved or Changes requested")
        item = self._item(req_id)
        try:
            self.ops().set_status_option(item.item_id, status)
        finally:
            self.invalidate()

    def add_comment(self, req_id: str, text: str, *, actor: str, kind: str, content_hash: str, reason: str | None = None) -> str:
        item = self._item(req_id)
        marker = encode_review_marker({"kind": kind, "actor": actor, "hash": content_hash, "at": _iso(self.clock()),
                                       "reqId": req_id, **({"reason": reason} if reason else {})})
        try:
            return self.ops().add_comment(item.issue_id, f"{text}\n\n{marker}")
        finally:
            self.invalidate()
