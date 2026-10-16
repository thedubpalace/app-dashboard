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

## Verify

Run `python -m unittest discover -s tests -v`.
