# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users
[Inferred from code — single confirmed user, not interviewed.] The sole user is the machine's owner/admin (thedubpalace), operating their own Windows homelab server. No other audience — this is a personal control panel, not a shared or multi-tenant product.

## Product Purpose
App Dashboard is a personal control panel for one Windows machine: start/stop/restart locally-registered apps, monitor and toggle Docker containers, monitor/start/stop VMware VMs, and toggle Tailscale connectivity. It exists so the owner doesn't have to open a terminal to check whether their self-hosted apps and services are alive, or to bounce them.

## Positioning
[Inferred.] Not a product with market positioning — a bespoke ops tool built to fit this one person's exact stack (a `registry.json` of their own apps, their own Docker containers, their own VMware VMs). Its edge over generic tools (Portainer, Docker Desktop, Task Scheduler) is that it unifies all of it — apps, containers, VMs, Tailscale, and even live Claude Code remote-control session counts per app — into one glance.

## Operating Context
- Runs as a small FastAPI server (`main.py`) on the same Windows box it manages, on port 8765, exposed publicly through an nginx-proxy-manager + DuckDNS tunnel at custom subdomains (e.g. bridgenbrain.duckdns.org).
- Single-page client (`templates/index.html`), fully client-rendered, polls `/api/apps`, `/api/docker`, `/api/vmware`, `/api/tailscale/status` every 10–15s.
- Sections: Remote Access (Tailscale connectivity and Sunshine restart), Apps (with a Status/Config tab toggle), Docker Containers (with a keyword filter), VMware VMs. A header holds a manual refresh button and a "restart machine" action.
- Used both from a desktop browser on the LAN and remotely over the internet (hence the duckdns domains) — so it needs to read cleanly on both desktop and mobile viewports.
- "Registered apps" are the owner's own side projects/tools (stock-pattern, matchday, vibecheck, skill-finder, powerpoint-generator, flight-deal-watcher, pourover-guide, asteroid-impact-simulation, app-dashboard itself, etc.), each with a name, optional description, run command, health check, and local path.

## Capabilities and Constraints
- Start/stop/restart a registered app (spawns/kills local processes); open a Claude Code remote-control shell for an app's directory; kill all Claude sessions tied to an app.
- Start/stop Docker containers; a "Fix Docker" action that restarts Docker Desktop when it's stuck.
- Start/stop VMware VMs via `vmrun.exe`.
- Toggle Tailscale up/down.
- Everything is a live status read (running/stopped) refreshed on a polling interval, not push/websocket.
- Runs on Windows only (uses `taskkill`, `vmrun.exe`, PowerShell, Windows process semantics) — no cross-platform constraint to preserve.

## Brand Commitments
None — no existing name/logo identity beyond the literal title "Dashboard" / "App Dashboard". Free to establish a visual identity.

## Evidence on Hand
No screenshots, testimonials, or marketing copy exist or are needed — this is an internal tool with no external audience. Do not fabricate any.

## Product Principles
1. Glanceability over density — the owner checks this to answer "is X alive?" in under a second, from across the room or on a phone.
2. One-tap control — every visible status has an adjacent action (start/stop/restart/connect) with clear pending/done feedback.
3. Trustworthy at a glance — status must never look ambiguous; a stopped/error state should be impossible to mistake for running.
4. Low-ceremony personal tool — no onboarding, no auth screens, no empty-state marketing; it's a utility the owner already knows how to use.

## Accessibility & Inclusion
[Not established — single sighted user, no stated requirement. Maintain reasonable contrast and readable type sizes as good practice, but no formal a11y standard is binding here.]
