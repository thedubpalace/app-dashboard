"""Narrow, signed machine client for the Temporal orchestrator.

The dashboard never receives Docker/VMware/host command primitives.  It can
only submit a stable resource id and one of the orchestrator's finite actions.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class TemporalClientError(RuntimeError):
    def __init__(self, message: str, status: int = 503, payload: dict[str, Any] | None = None):
        super().__init__(message)
        self.status = status
        self.payload = payload or {"ok": False, "error": message}


class TemporalOrchestratorClient:
    def __init__(self) -> None:
        self.base_url = os.getenv("TEMPORAL_ORCHESTRATOR_URL", "http://127.0.0.1:5016/temporal-ops").rstrip("/")
        self.token = os.getenv("DASHBOARD_SERVICE_TOKEN", "")
        self.actor_secret = os.getenv("DASHBOARD_ACTOR_SECRET", "")
        self.timeout = float(os.getenv("TEMPORAL_ORCHESTRATOR_TIMEOUT", "10"))

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None, *, actor: str = "dashboard", role: str = "operator") -> Any:
        if not self.token or not self.actor_secret:
            raise TemporalClientError("Temporal machine credentials are not configured", 503)
        body = json.dumps(payload, separators=(",", ":")).encode() if payload is not None else b""
        timestamp = str(int(time.time() * 1000))
        body_hash = hashlib.sha256(body).hexdigest()
        if role not in {"operator", "admin"}:
            raise TemporalClientError("invalid machine role", 500)
        signed = "\n".join((method, path, timestamp, actor, role, body_hash)).encode()
        signature = hmac.new(self.actor_secret.encode(), signed, hashlib.sha256).hexdigest()
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "X-Veda-Client": "veda-dashboard",
            "X-Veda-Actor": actor,
            "X-Veda-Role": role,
            "X-Veda-Timestamp": timestamp,
            "X-Veda-Signature": signature,
        }
        request = Request(f"{self.base_url}{path}", data=body if method != "GET" else None, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - URL is operator-configured
                return json.loads(response.read() or b"null")
        except HTTPError as exc:
            try:
                error = json.loads(exc.read() or b"{}")
            except (ValueError, json.JSONDecodeError):
                error = {"ok": False, "error": f"orchestrator returned HTTP {exc.code}"}
            raise TemporalClientError(str(error.get("error") or error.get("message") or exc.reason), exc.code, error) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise TemporalClientError(f"Temporal orchestrator unavailable: {exc}", 503) from exc

    def sync_registry(self, *, actor: str) -> dict[str, Any]:
        return self._request("POST", "/api/machine/registry/sync", {}, actor=actor)

    def resources(self, *, actor: str = "dashboard", role: str = "operator") -> dict[str, Any]:
        return self._request("GET", "/api/machine/resources", actor=actor, role=role)

    def workflows(self, *, actor: str = "dashboard") -> dict[str, Any]:
        return self._request("GET", "/api/machine/workflows", actor=actor)

    def audit(self, *, actor: str = "dashboard") -> dict[str, Any]:
        return self._request("GET", "/api/machine/audit", actor=actor)

    def submit(self, resource_id: str, action: str, idempotency_key: str, *, actor: str, target_mode: str | None = None, role: str = "operator") -> dict[str, Any]:
        payload: dict[str, Any] = {"resourceId": resource_id, "operation": action, "idempotencyKey": idempotency_key}
        if target_mode:
            payload["targetMode"] = target_mode
        return self._request("POST", "/api/machine/operations", payload, actor=actor, role=role)

    def record_interactive_event(self, action: str, target: str, result: str, idempotency_key: str, *, actor: str, session_count: int | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"action": action, "target": target, "result": result, "idempotencyKey": idempotency_key}
        if session_count is not None:
            payload["sessionCount"] = session_count
        return self._request("POST", "/api/machine/events", payload, actor=actor)


client = TemporalOrchestratorClient()
