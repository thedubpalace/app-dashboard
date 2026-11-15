"""In-memory fake of the GitHub GraphQL API used by the review adapter tests.

Implements only the operations `services/github_projects.py` sends,
dispatched by operation name. No network is used.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

STATUS_OPTIONS = ["Draft", "Needs review", "Approved", "Changes requested"]


class FakeGitHub:
    def __init__(self, *, login="thedubpalace", repo="thedubpalace/requirements", scopes="project, repo",
                 with_fields=True, status_options=None, page_size=2, app_field="App"):
        self.app_field = app_field
        self.login = login
        self.repo = repo
        self.scopes = scopes
        self.expiration: str | None = None
        self.page_size = page_size
        self.calls: list[tuple[str, dict]] = []
        self.tokens_seen: list[str] = []
        self.requests_seen: list[tuple[bytes, dict]] = []  # (raw body, headers without Authorization)
        self.fail: dict[str, tuple] = {}  # op name (or "*") -> ("http", status, headers) | ("gql", type)
        self.fields = {"Status": {"id": "F_status", "dataType": "SINGLE_SELECT",
                                  "options": {name: f"O_{i}" for i, name in enumerate(status_options or STATUS_OPTIONS)}}}
        if with_fields:
            self.fields["REQ-ID"] = {"id": "F_req", "dataType": "TEXT", "options": {}}
            self.fields[app_field] = {"id": "F_proj", "dataType": "TEXT", "options": {}}
        self.issues: dict[str, dict] = {}
        self.items: list[dict] = []
        self._seq = 0

    # ---------- helpers for tests ----------
    def _id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}_{self._seq}"

    def now(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def item_for(self, req_id: str) -> dict:
        return next(item for item in self.items if item["fields"].get("REQ-ID", {}).get("value") == req_id)

    def status_of(self, req_id: str) -> str | None:
        return self.item_for(req_id)["fields"].get("Status", {}).get("value")

    def set_status(self, req_id: str, status: str) -> None:
        self.item_for(req_id)["fields"]["Status"] = {"value": status, "updatedAt": self.now()}

    def issue_for(self, req_id: str) -> dict:
        return self.issues[self.item_for(req_id)["issue"]]

    def add_raw_item(self, *, req_id, project, status, repo=None, body="", typename="Issue", archived=False):
        issue_id = self._id("I")
        number = len(self.issues) + 1
        self.issues[issue_id] = {"id": issue_id, "number": number, "url": f"https://github.com/{repo or self.repo}/issues/{number}",
                                 "title": f"{req_id}: raw", "body": body, "repository": repo or self.repo, "comments": [],
                                 "__typename": typename}
        fields = {"REQ-ID": {"value": req_id, "updatedAt": self.now()}, self.app_field: {"value": project, "updatedAt": self.now()}}
        if status:
            fields["Status"] = {"value": status, "updatedAt": self.now()}
        self.items.append({"id": self._id("PVTI"), "issue": issue_id, "fields": fields, "archived": archived})

    def mutations(self) -> list[str]:
        return [name for name, _ in self.calls if name in {"ReviewSetField", "ReviewComment", "ReviewCreateIssue", "ReviewUpdateIssue", "ReviewAddItem", "ReviewCreateField"}]

    # ---------- transport ----------
    def __call__(self, url: str, body: bytes, headers: dict, timeout: float):
        auth = headers.get("Authorization", "")
        self.tokens_seen.append(auth)
        self.requests_seen.append((body, {k: v for k, v in headers.items() if k != "Authorization"}))
        payload = json.loads(body.decode("utf-8"))
        match = re.match(r"\s*(?:query|mutation)\s+(\w+)", payload["query"])
        name = match.group(1) if match else "?"
        variables = payload.get("variables") or {}
        self.calls.append((name, variables))
        response_headers = {"x-oauth-scopes": self.scopes, "x-ratelimit-remaining": "4999", "x-ratelimit-reset": "1900000000"}
        if self.expiration:
            response_headers["github-authentication-token-expiration"] = self.expiration
        fail = self.fail.get(name) or self.fail.get("*")
        if fail:
            if fail[0] == "http":
                return fail[1], {**response_headers, **(fail[2] if len(fail) > 2 else {})}, b'{"message":"nope"}'
            if fail[0] == "gql":
                return 200, response_headers, json.dumps({"data": None, "errors": [{"type": fail[1], "message": "x"}]}).encode()
        data = getattr(self, f"op_{name}")(variables)
        return 200, response_headers, json.dumps({"data": data}).encode()

    # ---------- operations ----------
    def op_ReviewViewer(self, v):
        return {"viewer": {"login": self.login}, "rateLimit": {"remaining": 4999, "resetAt": self.now()}}

    def _project(self):
        return {"id": "PVT_1", "title": "Requirements", "url": "https://github.com/users/thedubpalace/projects/1"}

    def op_ReviewProjectMeta(self, v):
        nodes = []
        for name, f in self.fields.items():
            node = {"__typename": "ProjectV2SingleSelectField" if f["dataType"] == "SINGLE_SELECT" else "ProjectV2Field",
                    "id": f["id"], "name": name, "dataType": f["dataType"]}
            if f["dataType"] == "SINGLE_SELECT":
                node["options"] = [{"id": oid, "name": oname} for oname, oid in f["options"].items()]
            nodes.append(node)
        return {"owner": {"projectV2": {**self._project(), "fields": {"nodes": nodes}}}}

    def op_ReviewRepo(self, v):
        if f"{v['owner']}/{v['name']}" != self.repo:
            return {"repository": None}
        return {"repository": {"id": "R_1", "nameWithOwner": self.repo, "isPrivate": True}}

    def _value(self, item, name):
        f = item["fields"].get(name)
        if not f or name not in self.fields:
            return None
        if self.fields[name]["dataType"] == "SINGLE_SELECT":
            return {"name": f["value"], "updatedAt": f["updatedAt"]}
        return {"text": f["value"], "updatedAt": f["updatedAt"]}

    def op_ReviewItems(self, v):
        start = int(v.get("cursor") or 0)
        page = self.items[start:start + self.page_size]
        nodes = []
        for item in page:
            issue = self.issues[item["issue"]]
            content = {"__typename": issue.get("__typename", "Issue")}
            if content["__typename"] == "Issue":
                content.update({"id": issue["id"], "number": issue["number"], "url": issue["url"], "title": issue["title"],
                                "body": issue["body"], "repository": {"nameWithOwner": issue["repository"]},
                                "comments": {"nodes": issue["comments"][-50:]}})
            nodes.append({"id": item["id"], "isArchived": item["archived"], "status": self._value(item, "Status"),
                          "reqId": self._value(item, "REQ-ID"), "app": self._value(item, v["appField"]), "content": content})
        end = start + len(page)
        return {"owner": {"projectV2": {"items": {"pageInfo": {"hasNextPage": end < len(self.items), "endCursor": str(end)}, "nodes": nodes}}}}

    def op_ReviewSetField(self, v):
        item = next(i for i in self.items if i["id"] == v["item"])
        name = next(n for n, f in self.fields.items() if f["id"] == v["field"])
        if "singleSelectOptionId" in v["value"]:
            value = next(o for o, oid in self.fields[name]["options"].items() if oid == v["value"]["singleSelectOptionId"])
        else:
            value = v["value"]["text"]
        item["fields"][name] = {"value": value, "updatedAt": self.now()}
        return {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": item["id"]}}}

    def op_ReviewComment(self, v):
        issue = self.issues[v["subject"]]
        comment = {"id": self._id("IC"), "body": v["body"], "createdAt": self.now(), "url": issue["url"] + "#c", "author": {"login": self.login}}
        issue["comments"].append(comment)
        return {"addComment": {"commentEdge": {"node": {"id": comment["id"], "url": comment["url"]}}}}

    def op_ReviewCreateIssue(self, v):
        issue_id = self._id("I")
        number = len(self.issues) + 1
        self.issues[issue_id] = {"id": issue_id, "number": number, "url": f"https://github.com/{self.repo}/issues/{number}",
                                 "title": v["title"], "body": v["body"], "repository": self.repo, "comments": []}
        return {"createIssue": {"issue": {"id": issue_id, "number": number, "url": self.issues[issue_id]["url"]}}}

    def op_ReviewUpdateIssue(self, v):
        self.issues[v["id"]].update(title=v["title"], body=v["body"])
        return {"updateIssue": {"issue": {"id": v["id"]}}}

    def op_ReviewAddItem(self, v):
        existing = next((i for i in self.items if i["issue"] == v["content"]), None)
        if existing:
            return {"addProjectV2ItemById": {"item": {"id": existing["id"]}}}
        item = {"id": self._id("PVTI"), "issue": v["content"], "fields": {}, "archived": False}
        self.items.append(item)
        return {"addProjectV2ItemById": {"item": {"id": item["id"]}}}

    def op_ReviewCreateField(self, v):
        fid = self._id("F")
        self.fields[v["name"]] = {"id": fid, "dataType": "TEXT", "options": {}}
        return {"createProjectV2Field": {"projectV2Field": {"id": fid, "name": v["name"]}}}
