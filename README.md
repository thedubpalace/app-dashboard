# Veda Dashboard

Read-only local inventory with an authenticated lifecycle facade. Every
enabled start/stop/restart/mode action is submitted to the Temporal Service
Orchestrator; this process never directly mutates Docker, VMware, or host
processes.

## Configuration

Copy `.env.example` values into the service environment (the app does not load
dotenv files itself). `DASHBOARD_SERVICE_TOKEN` and `DASHBOARD_ACTOR_SECRET`
must exactly match the orchestrator. Keep those two values independent. Set
`DASHBOARD_COOKIE_SECURE=1` behind HTTPS.

The orchestrator URL includes its public prefix, for example
`http://127.0.0.1:5016/temporal-ops`. Only its web/API port is consumed. Do not
expose Temporal UI, Temporal gRPC, Docker proxy endpoints, or Docker sockets to
this dashboard.

The Veda registry is the authorization source. Runtime discovery adds
`unmanaged` display rows but never enables actions. Registry entries connect
to this UI through exact `surfaceRefs` (`app`, `docker`, `vmware`, and
`app-mode`). Duplicate submissions use `Idempotency-Key` and return the same
workflow projection. Legacy mutation URLs remain available but return `202`
after queueing a workflow.

Monitoring remains a direct read path: loading or polling apps, containers,
VMs, and health never submits a workflow. Lifecycle feedback is row-local and
maps the durable state to Pending, Running, Done, or Failed. Submitted actions
include a server-built link to the official Temporal UI execution; the
`/temporal-ops/` link remains the secondary admin/policy console. Full workflow
and audit records remain available through their existing backend endpoints.

Local process lifecycle is enabled only for the exact signed
`process.skill-finder` and `process.html-hub` registry resources. Their Start,
Stop, and Restart buttons use Temporal's owned-process state rather than a port
match. If a health endpoint is served by a process that Temporal did not
launch, the row remains observable but lifecycle controls fail closed; the
dashboard never kills a PID, process name, or port directly.

Control readiness requires both a ready registry projection and live Temporal
plus worker heartbeat state. Resource capabilities are disabled server-side
when the required worker task queue is stale. A rejected submission remains as
the row's latest Failed action instead of being overwritten by an older
successful workflow; it clears when a newer workflow is accepted or observed.

```powershell
python -m pip install -r requirements.txt
python -m uvicorn main:app --host 127.0.0.1 --port 8765
python -m unittest discover -s tests
```

Shell launch, Codex/Claude session control, Docker Desktop repair, machine
restart, and Tailscale mutation return `501` until each has a dedicated exact
adapter. Their read-only status views remain available.

## Local service deployment

The dashboard runs as the limited-user Scheduled Task `Veda Dashboard`. The
launcher loads the ignored `.env.runtime` before starting uvicorn. The
launcher defaults to `0.0.0.0:8765`; production sets
`VEDA_DASHBOARD_HOST=192.168.1.37` so the local Nginx Proxy Manager can reach an
exact host address without exposing every interface. Use
`VEDA_DASHBOARD_HOST` and `VEDA_DASHBOARD_PORT` to override that bind. Restrict
inbound port 8765 to the trusted proxy/network boundary and use the public
HTTPS URL for authenticated sessions because the production cookie is
`Secure`.

`start.bat` delegates to the same managed launcher, so a manual start uses the
same runtime environment rather than creating an unconfigured second service.
`settings.json` and `.codex/` remain user-owned and deployment does not replace
them. Task Scheduler restarts the service after failure or logon.

## Requirement review (`/review`)

Human gate for Veda requirements (REQ-001..018 in `.veda/projects/app-dashboard/requirements.md`).

- Access: signed-in dashboard session, role Admin (`DASHBOARD_USER_ROLES`, default: the configured
  `VEDA_DASHBOARD_USERNAME` is Admin) and listed in `REVIEW_ALLOWLIST` (default `thedubpalace`).
  Mutations need the existing `X-CSRF-Token`. 401/403 attempts go to `.runtime/security.log`.
- Tracker: `REVIEW_TRACKER=mock|github` (default `mock`).
  - `mock`: cards are parsed from the real `requirements.md` (`REVIEW_MOCK_PROJECTS`, default
    `app-dashboard`; projects dir from `REVIEW_VEDA_PROJECTS_DIR` or the registry folder). Status,
    comments, idempotency, gate result and audit live in `.runtime/review-sandbox/` (ignored).
    `registry.json` is never written; the page shows what reconcile *would* set.
    Admins get a debug panel to simulate GitHub/token/two-phase/conflict states.
  - `github`: `GitHubProjectTracker` skeleton only. It fails closed (`token_missing`) until implemented.
- REQ block format parsed from requirements.md: a line `**REQ-NNN Title**`, bullet lines, and
  `- AC: (a) ... (b) ...`. The content hash covers the whole block.
- Token (github mode, not used yet): `REVIEW_GITHUB_TOKEN` in the ignored `.env.runtime`, plus
  `REVIEW_GITHUB_OWNER`, `REVIEW_GITHUB_PROJECT_NUMBER`, `REVIEW_GITHUB_REPO`. Never commit it; it is
  never sent to the browser. Rotate: create a new token, replace the value, restart the dashboard,
  revoke the old token. Scope to confirm when connecting: fine-grained PATs may not reach a
  user-owned ProjectV2; fallback is a classic PAT with `project` + `repo` (REQ-016).
- Other settings: `REVIEW_REQUIRED_APPROVALS` (default 1), `REVIEW_RUNTIME_DIR`, `REVIEW_AUDIT_PATH`.
