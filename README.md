# App Dashboard

A personal control panel for registered apps, Docker containers, VMware VMs,
and Tailscale.

## Run locally

Install `requirements.txt`, then run `start.bat` or `start-service.ps1` on Windows.
The dashboard listens on `http://localhost:8765` by default.

`start-service.ps1` loads `.env.runtime` when present and supports these settings:

| Variable | Default |
| --- | --- |
| `APP_DASHBOARD_HOST` | `0.0.0.0` |
| `APP_DASHBOARD_PORT` | `8765` |
| `APP_DASHBOARD_REGISTRY` | `../veda/.veda/projects/registry.json` |

The launcher validates the configured port, checks Python dependencies, and reports
an already-running dashboard instead of starting a second server on the same port.
Use `0.0.0.0` for the host to support both localhost and LAN connections.

Registry paths are relative to the repository's parent directory by default.
The existing shared registry stays in place. `VEDA_DASHBOARD_HOST`,
`VEDA_DASHBOARD_PORT`, and `VEDA_REGISTRY` remain supported as fallbacks;
the new variables take precedence.

## Sign-in, roles and audit

Every `/api` call needs a signed session cookie; every state-changing call also
needs the session's CSRF token in `X-CSRF-Token`, a same-origin `Origin` (when
the browser sends one) and a role that holds the route's permission. The table
is `services/access_policy.py`; `services/access_guard.py` enforces it before
routing, so it also covers the `/api` relay to the host agent. Routes missing
from the table are denied (403) even for Admin, and
`tests/test_access_policy.py` fails while any registered route has no rule.

Public without a session: the page shells `/` and `/review` (they only render
the sign-in form), `POST /api/login`, `GET /api/session` and
`GET /api/control/readiness` (booleans for health probes).

| Role | Permissions |
| --- | --- |
| viewer | read inventory (apps, Docker, VMware, Tailscale, settings) |
| operator | viewer + start/stop/restart/mode of registered resources, Docker filter |
| admin | operator + Claude/Codex terminals, host reboot, Sunshine, Docker repair, Tailscale |

`DASHBOARD_USER_ROLES=alice:admin,bob:viewer` maps users to roles. Without a
mapping the configured `VEDA_DASHBOARD_USERNAME` is Admin and anyone else is a
Viewer. `/review` additionally needs Admin plus `REVIEW_ALLOWLIST`.

Each authorised mutation writes a `requested` record before it runs (if that
fails, nothing runs and the call answers 503) and a `result` record with the
HTTP status to `.runtime/audit.jsonl`. Every 401/403 and failed sign-in goes to
`.runtime/security.log`. Neither holds bodies, cookies, tokens or passwords.

Settings (in the ignored `.env.runtime`; see `.env.example`):

| Variable | Purpose |
| --- | --- |
| `VEDA_DASHBOARD_USERNAME`, `VEDA_DASHBOARD_PASSWORD` | the dashboard account (sign-in is disabled without a password) |
| `VEDA_DASHBOARD_SESSION_SECRET` | HMAC key for session cookies (32+ random bytes) |
| `DASHBOARD_COOKIE_SECURE` | `1` when browsers reach the dashboard over HTTPS |
| `DASHBOARD_LOGIN_RATE_ATTEMPTS`, `DASHBOARD_LOGIN_RATE_WINDOW_SECONDS` | sign-in throttle (default 5 per 900 s) |
| `DASHBOARD_ALLOWED_ORIGINS` | extra `host[:port]` values accepted as `Origin` |
| `DASHBOARD_AUDIT_LOG`, `DASHBOARD_SECURITY_LOG` | log paths |
| `TEMPORAL_ORCHESTRATOR_URL`, `DASHBOARD_SERVICE_TOKEN`, `DASHBOARD_ACTOR_SECRET` | signed machine channel to temporal-service-orchestrator, used by readiness |
| `APP_DASHBOARD_RECONCILER` | `0` disables the reconciler (staging copies next to the live service) |

`/api/control/readiness` always answers 200; `ok` is true only when the
orchestrator's registry sync is `ready` and Temporal and its host worker are up.

## Host agent (optional split)

By default one process serves the page and acts on the host. The dashboard
can instead be split in two:

- **Host agent** (`agent.py`, port `8766`) runs natively and does everything
  that touches the host. It requires a bearer token on every `/api` route and
  runs the reconciler.
- **Dashboard UI** (`main.py` with `APP_AGENT_URL` set) serves the page and
  relays `/api` to the agent, adding the token server side. It can run
  natively or in Docker. If the agent is down, the page still loads and API
  calls fail with `agentOffline: true`.

Start the agent with `start-agent.ps1`. To run it at logon and restart it if it
exits, run `install-agent-task.ps1` once (`-Remove` deletes the task). It runs
as a scheduled task in your session, not as a Windows service, because it opens
terminals and starts dev servers on your desktop.

| Variable | Default | Used by |
| --- | --- | --- |
| `APP_AGENT_HOST` | `127.0.0.1` | agent |
| `APP_AGENT_PORT` | `8766` | agent |
| `APP_AGENT_TOKEN` | contents of `.agent-token` | agent, UI |
| `APP_AGENT_URL` | unset (all-in-one) | UI |

On first start the agent writes a random token to `.agent-token` (git-ignored).
A native UI on the same host reads it from there. For the UI to use the agent
natively, add `APP_AGENT_URL=http://127.0.0.1:8766` to `.env.runtime`.

To run the UI in Docker, copy the token into `.env` as `APP_AGENT_TOKEN=...`,
stop the native dashboard on port 8765, and run `docker compose up -d --build`.
Docker Desktop reaches an agent bound to `127.0.0.1` through
`host.docker.internal`, so the agent does not need to listen on the LAN.
If the `App Dashboard Health Watchdog` task (in `infra/dashboard-watchdog`)
is installed, reinstall it with `-DashboardMode Docker` so it restarts the
container instead of starting a native server on port 8765.

### Reconciler

The reconciler checks every 60 seconds. It runs in the agent, or in the
all-in-one process when there is no agent.

- VMs with `"desiredRunning": true` under `vmware.managed` are started if
  they are down.
- Apps opt in with `"desiredRunning": true` under `apps.<name>`. A desired app
  that is down is started, with at most one retry per app every 5 minutes.
  The dashboard's Stop button sets it to `false` so a deliberate stop sticks,
  and Start or Restart sets it back to `true`. Apps without the key are never
  started automatically.

The aggregate `/api/status` endpoint returns `apps`, `docker`, and `vmware`.
Consumers of its former `veda` field should use `apps`. The individual
`/api/apps` routes are unchanged.

Per-app settings in `settings.json` can select a `devCmd` with that project's Python
environment and a `mode` of `dev` or `docker`. For stacks with dependencies,
`dockerStartCmd` runs from the project's `localPath` before Docker start/restart.
Temporal uses this to restore Compose services and the Windows worker together.

App links are discovered from Nginx Proxy Manager's active configuration, including
subpaths, variable upstreams, and routes to other machines. Registered public URLs
and public health checks identify the matching proxy location. Apps without a
matching proxy route are left without a public link.

Stock Pattern is a finite CLI job. Its process normally exits when its report is
complete; a stopped indicator does not imply a startup failure. VibeCheck runs as
the `vibecheck` Docker container (web UI + MCP at `/vibecheck/`).

## Layout

- `main.py` is the dashboard: the page, plus `/api` either in-process or
  relayed to the agent.
- `agent.py` is the host agent: `/api` behind a token, plus the reconciler.
- `routers/` holds the HTTP routes. They only validate requests and relay results.
- `services/` holds everything that acts on the host: processes, Docker,
  VMware, Nginx Proxy Manager, Sunshine, Tailscale, and `settings.json`.

## Verify

The **Remote Access** section groups Tailscale and Sunshine controls.
Sunshine's **Restart** button restarts the local Windows
`SunshineService` and waits for it to report Running. It asks for confirmation
because active Moonlight streams disconnect during the restart.

If Windows denies service control, run this once in an Administrator PowerShell,
using the account that runs App Dashboard:

```powershell
.\setup-sunshine-control.ps1 -Account 'MINIPC-KONG\thedu'
```

This grants that account query-status, start, and stop permissions for Sunshine
only. The dashboard itself does not need to run as Administrator.

Run `python -m unittest discover -s tests -v`.

## Requirement review (`/review`)

Human gate for Veda requirements (REQ-001..018 in `.veda/projects/app-dashboard/requirements.md`).

- Access: signed-in dashboard session, role Admin and listed in `REVIEW_ALLOWLIST` (default `thedubpalace`).
  Mutations need the `X-CSRF-Token`. 401/403 attempts go to `.runtime/security.log`.
- Tracker: `REVIEW_TRACKER=mock|github` (default `mock`).
  - `mock`: cards are parsed from the real `requirements.md` (`REVIEW_MOCK_PROJECTS`, default
    `app-dashboard`; projects dir from `REVIEW_VEDA_PROJECTS_DIR` or the registry folder). Status,
    comments, idempotency, gate result and audit live in `.runtime/review-sandbox/` (ignored).
    `registry.json` is never written; the page shows what reconcile *would* set and a banner that
    the data is simulated. Admins get a debug panel to simulate GitHub/token/two-phase/conflict states.
  - `github`: `services/github_projects.py` (the only module with GitHub API code). Cards are
    the issues on the ProjectV2 board; content is always rendered from the local `requirements.md`
    (source of truth), status and decision comments come from GitHub. A card whose local content
    differs from the issue body (sync not run yet) cannot be approved; an Approved card whose
    content changed counts as Draft. The queue is cached for at most `REVIEW_GITHUB_CACHE_SECONDS`
    (default 20, max 60); every decision re-fetches.
- Gate (github mode): after every decision and on every queue open, the reconcile writes only
  `requirementsReady` and `requirementsApproval` of each project that has cards, through a targeted,
  locked, idempotent edit of `registry.json` (`services/registry_writer.py`; no other byte changes).
  Projects without cards keep their value (`requirementsApproval: "legacy"` = grandfathered).
  **Backfilling a legacy project (running sync for it) puts it under the gate: its flag becomes
  false until every REQ is approved.** `REVIEW_REGISTRY_WRITE=0` disables the write. Committing
  `registry.json` into the veda repo stays a separate, manual step.
- Audit: github mode appends to `.veda/audit/requirement-reviews.jsonl`; the sync logs to
  `.veda/audit/requirement-sync.jsonl` (creates, updates, drift overwrites, withdrawals).

### GitHub token (REQ-016)

Checked 2026-11-15 on docs.github.com ("Managing your personal access tokens" > fine-grained
limitations; "Using the API to manage Projects"): **fine-grained PATs cannot access Projects owned
by a user account** (account permissions have no Projects entry), and the Projects API docs name only
classic PATs and GitHub App installation tokens. GitHub Apps also cannot act on user-owned projects.

- User-owned project (default `thedubpalace`): **classic PAT with scopes `project` and `repo`**
  (`repo` is needed because the central repo is private; `public_repo` is enough for a public repo).
  Set an expiry (e.g. 90 days). The dashboard rejects a `github_pat_...` token in this mode with a
  clear message instead of failing later.
- Least-privilege alternative: move the Project and the central repo to a free GitHub organization,
  set `REVIEW_GITHUB_OWNER=<org>` and `REVIEW_GITHUB_OWNER_TYPE=organization`, and use a fine-grained
  PAT limited to that repo (Issues: read/write, Metadata: read) with organization permission
  Projects: read/write.
- Storage: `REVIEW_GITHUB_TOKEN=...` in the ignored `.env.runtime` **only**. The dashboard and the
  sync script read it from that file, never from the process environment; `start-service.ps1` and
  `start-agent.ps1` do not export it, so launched consoles do not inherit it. It is never logged,
  put in an error, an audit line or a response.
- Expiry: the `github-authentication-token-expiration` response header drives a warning on `/review`
  `REVIEW_TOKEN_WARNING_DAYS` (default 14) days before expiry; an expired/revoked token shows the
  token state and disables every action.
- Rotate: create the new classic PAT, replace the value in `.env.runtime`, run
  `scripts/review_sync.py check`, restart the dashboard (`start-service.ps1`), then delete the old
  token on GitHub.

### Setup (once) and sync

Settings in `.env.runtime`:

```env
REVIEW_TRACKER=github
REVIEW_GITHUB_TOKEN=ghp_...            # classic PAT, scopes: project, repo
REVIEW_GITHUB_OWNER=thedubpalace
REVIEW_GITHUB_OWNER_TYPE=user          # or organization
REVIEW_GITHUB_PROJECT_NUMBER=<n>       # from https://github.com/users/<owner>/projects/<n>
REVIEW_GITHUB_REPO=thedubpalace/veda-requirements
```

1. On GitHub: create the private repo `veda-requirements` and a user Project "Veda Requirements".
   Edit its built-in **Status** field to exactly four options: `Draft`, `Needs review`, `Approved`,
   `Changes requested` (other values are ignored by `/review`).
2. `.venv\Scripts\python.exe scripts\review_sync.py check` - token, scopes, repo, project, fields.
3. `... scripts\review_sync.py bootstrap --dry-run`, then without `--dry-run`: creates the text
   fields `REQ-ID` and `Veda Project` when missing and lists anything left to fix by hand.
4. `... scripts\review_sync.py sync --project <name> --dry-run`, then without `--dry-run`: creates
   one issue `REQ-NNN: <title>` per block, adds it to the project with Status `Needs review`, and
   records the mapping in `.veda/requirements-index.json`. Re-running is idempotent. The sync can only
   set `Draft` / `Needs review`; it never approves.
5. Set `REVIEW_TRACKER=github` and restart the dashboard. Until then the default stays `mock`.

Offline helpers: `review_sync.py index --project <name>` (record local REQ blocks in the index) and
`review_sync.py allocate --project <name> --count N` (issue new global REQ ids under a file lock).

Requirement block format (parsed by `services/review_parser.py`): a line `**REQ-NNN Title**`,
then bullet lines, with acceptance criteria on a bullet `- AC: (a) ... (b) ...`. The block ends at
the next REQ line or heading. Ids are global, never reused; commits/PRs reference them (`REQ-042`).
