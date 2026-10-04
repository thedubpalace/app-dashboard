(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.VedaStatusModel = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const ACTIVE_STALE_MS = 30 * 60 * 1000;
  const ACTIVE_STATES = new Set(["pending", "running", "retrying", "queued"]);

  function words(value) {
    return String(value || "")
      .trim()
      .replace(/[_-]+/g, " ")
      .replace(/\b\w/g, (letter) => letter.toUpperCase());
  }

  function observedStatus(item) {
    const raw = String(item?.status || "").trim().toLowerCase();
    if (item?.statusStale === true || raw === "stale") return { key: "stale", label: "Stale", tone: "unknown" };
    if (["running", "online", "up", "connected"].includes(raw)) return { key: "running", label: "Running", tone: "on" };
    if (["stopped", "offline", "down", "disconnected", "exited"].includes(raw)) return { key: "stopped", label: "Stopped", tone: "off" };
    return { key: "unknown", label: "Unknown", tone: "unknown" };
  }

  function controlStatus(value) {
    const raw = String(value || "").trim().toLowerCase();
    if (["running", "online", "up", "connected"].includes(raw)) return { key: "running", label: "Running" };
    if (["stopped", "offline", "down", "disconnected", "exited"].includes(raw)) return { key: "stopped", label: "Stopped" };
    if (raw === "unavailable") return { key: "unavailable", label: "Unavailable" };
    if (raw === "stale" || raw === "policy-stale") return { key: "stale", label: "Stale" };
    return { key: "unknown", label: "Unknown" };
  }

  function transitionLabel(operation) {
    const op = String(operation || "").trim().toLowerCase();
    return ({ start: "Starting", stop: "Stopping", restart: "Restarting", deploy: "Deploying", "switch-mode": "Switching" })[op] || words(op || "Running");
  }

  function terminalTarget(operation, item, message) {
    if (/rollback|restored|previous state/i.test(String(message || ""))) return { key: "restored", label: "Restored" };
    const op = String(operation || "").toLowerCase();
    if (op === "start" || op === "restart") return { key: "running", label: "Running" };
    if (op === "stop") return { key: "stopped", label: "Stopped" };
    return controlStatus(item?.controlStatus);
  }

  function temporalStatus(item, action, nowMs) {
    if (!action) {
      if (!item || item.controlStatus === undefined || item.controlStatus === null || item.controlStatus === "") return null;
      return { ...controlStatus(item.controlStatus), state: "observed", message: "", busy: false, temporalUrl: "" };
    }
    const state = String(action.state || "").trim().toLowerCase();
    const updated = Date.parse(String(action.updatedAt || ""));
    if (ACTIVE_STATES.has(state) && (!Number.isFinite(updated) || nowMs - updated > ACTIVE_STALE_MS)) {
      return { key: "stale", label: "Stale", state: "stale", message: "Workflow status has not updated", busy: false, temporalUrl: action.temporalUrl || "" };
    }
    if (ACTIVE_STATES.has(state)) {
      const message = /retry|attempt/i.test(String(action.message || "")) ? String(action.message) : "";
      return { key: `transition:${action.operation || "unknown"}`, label: transitionLabel(action.operation), state: state === "pending" || state === "queued" ? "pending" : "running", message, busy: true, temporalUrl: action.temporalUrl || "" };
    }
    if (["failed", "rejected", "timed out", "timeout", "canceled", "cancelled"].includes(state)) {
      return { key: "failed", label: state === "rejected" ? "Rejected" : state.startsWith("cancel") ? "Cancelled" : state.startsWith("time") ? "Timed out" : "Failed", state: "failed", message: String(action.message || ""), busy: false, temporalUrl: action.temporalUrl || "" };
    }
    if (["done", "succeeded", "completed"].includes(state)) {
      return { ...terminalTarget(action.operation, item, action.message), state: "done", message: "", busy: false, temporalUrl: action.temporalUrl || "" };
    }
    return { key: "unknown", label: "Unknown", state: "unknown", message: "Unrecognized workflow state", busy: false, temporalUrl: action.temporalUrl || "" };
  }

  function merge(item, action, nowMs = Date.now()) {
    const primary = observedStatus(item);
    const temporal = temporalStatus(item, action, nowMs);
    const matches = Boolean(temporal && temporal.key === primary.key);
    return {
      primary,
      secondary: temporal && !matches ? `Temporal: ${temporal.label}` : "",
      secondaryState: temporal?.state || "",
      message: temporal && !matches ? temporal.message : "",
      temporalUrl: temporal?.temporalUrl || "",
      busy: Boolean(temporal?.busy),
      matches,
    };
  }

  return { ACTIVE_STALE_MS, merge, observedStatus };
});
