const test = require("node:test");
const assert = require("node:assert/strict");
const { ACTIVE_STALE_MS, merge } = require("../static/status-model.js");

const NOW = Date.parse("2026-10-04T12:00:00Z");

test("matching observed and terminal Temporal states collapse to one primary status", () => {
  const result = merge(
    { status: "running", controlStatus: "Running" },
    { operation: "start", state: "Done", updatedAt: "2026-10-04T11:59:00Z", temporalUrl: "/temporal-ui/workflow" },
    NOW,
  );
  assert.equal(result.primary.label, "Running");
  assert.equal(result.secondary, "");
  assert.equal(result.temporalUrl, "/temporal-ui/workflow");
  assert.equal(result.matches, true);
});

test("active lifecycle conflict keeps observed state primary and Temporal transition secondary", () => {
  const result = merge(
    { status: "stopped", controlStatus: "Stopped" },
    { operation: "start", state: "Running", updatedAt: "2026-10-04T11:59:00Z", temporalUrl: "/temporal-ui/workflow" },
    NOW,
  );
  assert.equal(result.primary.label, "Stopped");
  assert.equal(result.secondary, "Temporal: Starting");
  assert.equal(result.busy, true);
  assert.equal(result.temporalUrl, "/temporal-ui/workflow");
});

test("failed workflow remains visible without replacing live observed health", () => {
  const result = merge(
    { status: "running", controlStatus: "Running" },
    { operation: "restart", state: "Failed", message: "Health confirmation timed out", updatedAt: "2026-10-04T11:59:00Z" },
    NOW,
  );
  assert.equal(result.primary.label, "Running");
  assert.equal(result.secondary, "Temporal: Failed");
  assert.equal(result.message, "Health confirmation timed out");
});

test("stale active workflow and unknown control state are explicit", () => {
  const stale = merge(
    { status: "stopped", controlStatus: "Stopped" },
    { operation: "start", state: "Running", updatedAt: new Date(NOW - ACTIVE_STALE_MS - 1).toISOString() },
    NOW,
  );
  assert.equal(stale.secondary, "Temporal: Stale");
  assert.equal(stale.busy, false);

  const unknown = merge({ status: "running", controlStatus: "Unknown" }, null, NOW);
  assert.equal(unknown.primary.label, "Running");
  assert.equal(unknown.secondary, "");
});

test("matching direct and projected states have no duplicate secondary label", () => {
  const result = merge({ status: "stopped", controlStatus: "Stopped" }, null, NOW);
  assert.equal(result.primary.label, "Stopped");
  assert.equal(result.secondary, "");
});

test("matching Missing observation and Temporal projection show one explicit status", () => {
  const result = merge({ status: "missing", controlStatus: "Missing" }, null, NOW);
  assert.equal(result.primary.label, "Missing");
  assert.equal(result.secondary, "");
});
