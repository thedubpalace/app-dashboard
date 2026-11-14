"""Who may call which route: the dashboard's single access table.

Every request the UI process serves is checked against RULES before routing:

- PUBLIC rules need nothing (the page shells, login, session probe, readiness).
- Every other rule needs a signed session (401), and a mutation also needs the
  session's CSRF token in ``X-CSRF-Token`` (403).
- The rule's permission must be granted to the user's role (403).
- Anything not listed is denied (403), even for Admin. A new route therefore
  stays closed until it is added here, and ``tests/test_access_policy.py``
  fails while any registered route has no rule.

Rules use the same path templates as the routers, so the table also covers the
``/api`` relay to the host agent (UI-only mode), which matches by real path.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from starlette.routing import compile_path

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

PUBLIC = "public"
# Permissions whose role check is done by the handler itself. The middleware
# still requires session + CSRF; services/review_api.py then requires Admin +
# the reviewer allowlist (and logs its own 403s).
REQUIREMENT_REVIEW = "requirement.review"
DELEGATED_PERMISSIONS = frozenset({REQUIREMENT_REVIEW})

INVENTORY_READ = "inventory.read"          # list apps/containers/VMs/Tailscale/settings
LIFECYCLE = "lifecycle.operate"            # start/stop/restart/mode of registered apps, containers, VMs
SETTINGS_WRITE = "settings.write"          # dashboard preferences (Docker filter)
TERMINAL = "interactive.terminal"          # open Claude/Codex terminals on the host, end them
SYSTEM_ADMIN = "system.admin"              # reboot the host, restart Sunshine, repair Docker Desktop
NETWORK_ADMIN = "network.admin"            # connect/disconnect Tailscale
SESSION_SELF = "session.self"              # sign out

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "viewer": frozenset({INVENTORY_READ, SESSION_SELF}),
    "operator": frozenset({INVENTORY_READ, SESSION_SELF, LIFECYCLE, SETTINGS_WRITE}),
    "admin": frozenset({INVENTORY_READ, SESSION_SELF, LIFECYCLE, SETTINGS_WRITE, TERMINAL, SYSTEM_ADMIN, NETWORK_ADMIN}),
}


@dataclass(frozen=True)
class Rule:
    method: str
    template: str
    permission: str
    regex: re.Pattern = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "regex", compile_path(self.template)[0])

    @property
    def mutation(self) -> bool:
        return self.method not in SAFE_METHODS


def _rules(permission: str, method: str, *templates: str) -> list[Rule]:
    return [Rule(method, template, permission) for template in templates]


RULES: list[Rule] = [
    # Page shells carry no data; they render a sign-in form until /api/session says otherwise.
    *_rules(PUBLIC, "GET", "/", "/review"),
    *_rules(PUBLIC, "POST", "/api/login"),
    *_rules(PUBLIC, "GET", "/api/session"),
    # Health probe for monitor.ps1, the watchdog and the Temporal orchestrator. Booleans only.
    *_rules(PUBLIC, "GET", "/api/control/readiness"),
    *_rules(SESSION_SELF, "POST", "/api/logout"),
    # Inventory (exposes host details, so it needs a session).
    *_rules(INVENTORY_READ, "GET", "/api/apps", "/api/docker", "/api/vmware", "/api/status", "/api/settings", "/api/tailscale/status"),
    # Lifecycle of registered resources.
    *_rules(LIFECYCLE, "POST",
            "/api/apps/{name}/start", "/api/apps/{name}/stop", "/api/apps/{name}/restart", "/api/apps/{name}/mode",
            "/api/docker/{name}/start", "/api/docker/{name}/stop", "/api/docker/{name}/restart",
            "/api/vmware/start", "/api/vmware/stop"),
    *_rules(SETTINGS_WRITE, "PATCH", "/api/settings/docker/filter"),
    # Interactive terminals run arbitrary tools on the host: Admin only.
    *_rules(TERMINAL, "POST",
            "/api/apps/{name}/shell", "/api/apps/{name}/codex",
            "/api/apps/{name}/claude-sessions/kill-all", "/api/apps/{name}/codex-sessions/kill-all"),
    *_rules(SYSTEM_ADMIN, "POST", "/api/system/restart", "/api/system/sunshine/restart", "/api/docker/fix"),
    *_rules(NETWORK_ADMIN, "POST", "/api/tailscale/toggle"),
    # Requirement review: session (+ CSRF on mutations) here, Admin + allowlist in services/review_api.py.
    *_rules(REQUIREMENT_REVIEW, "GET", "/api/review/queue", "/api/review/summary", "/api/review/cards/{req_id}", "/api/review/debug"),
    *_rules(REQUIREMENT_REVIEW, "POST",
            "/api/review/cards/{req_id}/approve", "/api/review/cards/{req_id}/request-changes",
            "/api/review/reconcile", "/api/review/debug"),
]


def match(method: str, path: str) -> Rule | None:
    """The rule for this request, or None (deny). HEAD is answered like GET."""
    lookup = "GET" if method == "HEAD" else method
    for rule in RULES:
        if rule.method == lookup and rule.regex.match(path):
            return rule
    return None


def is_static(method: str, path: str) -> bool:
    return method in {"GET", "HEAD"} and path.startswith("/static/") and ".." not in path


def allowed(role: str, permission: str) -> bool:
    if permission in DELEGATED_PERMISSIONS:
        return role in ROLE_PERMISSIONS  # any signed-in role reaches the handler, which decides
    return permission in ROLE_PERMISSIONS.get(role, frozenset())
