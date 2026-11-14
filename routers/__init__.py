"""HTTP routes. Each module only translates requests to calls into services/."""
from routers import apps, docker, settings, status, system, vmware

# Everything under /api — served by the host agent, or in-process by main.py
# when no agent is configured.
API_ROUTERS = [m.router for m in (settings, status, apps, docker, system, vmware)]
