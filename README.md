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

```powershell
python -m pip install -r requirements.txt
python -m uvicorn main:app --host 127.0.0.1 --port 8765
python -m unittest discover -s tests
```

Shell launch, Codex/Claude session control, Docker Desktop repair, machine
restart, and Tailscale mutation return `501` until each has a dedicated exact
adapter. Their read-only status views remain available.

## Local service deployment

The dashboard runs as the limited-user Scheduled Task `Veda Dashboard` and
listens only on `127.0.0.1:8765`. The task loads ignored `.env.runtime`; its
machine credentials match Temporal and its session secret is independent.
`settings.json` and `.codex/` remain user-owned and deployment does not replace
them. Task Scheduler restarts the service after failure or logon.
