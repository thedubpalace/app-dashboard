# App Dashboard

A personal control panel for registered apps, Docker containers, VMware VMs,
and Tailscale.

## Run locally

Install `requirements.txt`, then run `start.bat` or `start-service.ps1` on Windows.
The dashboard listens on `http://localhost:8765` by default.

`start-service.ps1` loads `.env.runtime` and supports these settings:

| Variable | Default |
| --- | --- |
| `APP_DASHBOARD_HOST` | `0.0.0.0` |
| `APP_DASHBOARD_PORT` | `8765` |
| `APP_DASHBOARD_REGISTRY` | `../veda/.veda/projects/registry.json` |

Registry paths are relative to the repository's parent directory by default.
The existing shared registry stays in place. `VEDA_DASHBOARD_HOST`,
`VEDA_DASHBOARD_PORT`, and `VEDA_REGISTRY` remain supported as fallbacks;
the new variables take precedence.

For Docker, the registry defaults to `/veda/.veda/projects/registry.json`.
Mount the existing registry there or set `APP_DASHBOARD_REGISTRY` to its
container path.

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

Stock Pattern and VibeCheck are finite CLI jobs. Their processes normally exit when
their reports are complete; a stopped indicator does not imply a startup failure.

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
