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
