"""Attach public links (as routed by Nginx Proxy Manager) to apps and containers."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from services import app_modes, nginx_proxy

_PUBLISHED_PORT = re.compile(r":(\d+)->")


def _address(url: str) -> tuple[str | None, int | None]:
    parsed = urlparse(url)
    return parsed.hostname, parsed.port or {"http": 80, "https": 443}.get(parsed.scheme)


def attach_public_urls(apps: list[dict[str, Any]]) -> None:
    """Link each app to the address it is reachable at from outside. Dev and
    container now share a port per app, so the same link works in either mode."""
    urls = nginx_proxy.public_urls()
    routes = nginx_proxy.public_routes()

    for entry in apps:
        entry.pop("url", None)
        declared = entry.get("publicUrl")
        if declared:
            match = next((r["url"] for r in routes if _address(r["url"]) == _address(declared)
                          and urlparse(r["url"]).path.rstrip('/') == urlparse(declared).path.rstrip('/')), None)
            if match:
                entry["url"] = match
                continue
        health = entry.get("healthCheck") or {}
        matches = []
        for target in (health.get("publicTarget"), health.get("target")):
            if not isinstance(target, str) or '://' not in target:
                continue
            for route in routes:
                prefix = urlparse(route["url"]).path.rstrip('/')
                path = urlparse(target).path
                if _address(route["url"]) == _address(target) and (not prefix or path == prefix or path.startswith(prefix + '/')):
                    matches.append(route["url"])
        if matches:
            entry["url"] = max(matches, key=lambda url: len(urlparse(url).path))
            continue
        port = entry.get("port") or app_modes.dev_port(entry)
        if port:
            if port in urls:
                entry["url"] = urls[port]
            else:
                # A unique remote route also identifies VM-hosted apps such
                # as Home Assistant. Never guess when several hosts share a port.
                candidates = {r["url"] for r in routes if r["port"] == port}
                if len(candidates) == 1:
                    entry["url"] = candidates.pop()


def attach_container_urls(containers: list[dict[str, Any]]) -> None:
    urls = nginx_proxy.public_urls()
    if not urls:
        return
    for container in containers:
        candidates: list[str] = []
        for match in _PUBLISHED_PORT.finditer(container.get("ports") or ""):
            url = urls.get(int(match.group(1)))
            if url and url not in candidates:
                candidates.append(url)
        if not candidates:
            continue
        # A container can publish several ports — pourover-guide-app answers on
        # both the domain root and /coffee. The path-scoped route names this app
        # specifically, so it beats a root catch-all.
        container["url"] = next(
            (u for u in candidates if urlparse(u).path.strip("/")), candidates[0]
        )
