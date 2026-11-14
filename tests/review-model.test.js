const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const M = require("../static/review-model.js");

const cards = (...ids) => ids.map((reqId) => ({ reqId, hash: `h-${reqId}`, incomplete: [] }));

test("counter shows position of total and shrinks when a card is decided", () => {
  let q = M.createQueue(cards("A", "B", "C", "D"));
  assert.deepEqual([M.view(q).position, M.view(q).total], [1, 4]);
  q = M.skip(q).state;
  assert.deepEqual([M.view(q).current.reqId, M.view(q).position, M.view(q).total], ["B", 2, 4]);
  q = M.decided(q, "B");
  assert.deepEqual([M.view(q).current.reqId, M.view(q).position, M.view(q).total], ["C", 2, 3]);
});

test("skip moves the card behind the queue and only skipped cards leaves the skipped-only state", () => {
  let q = M.createQueue(cards("A", "B"));
  q = M.skip(q).state;
  q = M.skip(q).state;
  const v = M.view(q);
  assert.equal(v.mode, "skipped-only");
  assert.equal(v.skippedCount, 2);
  assert.deepEqual(v.skippedIds, ["A", "B"]);
  q = M.restart(q);
  assert.equal(M.view(q).mode, "ready");
  assert.equal(M.view(q).skippedCount, 0);
  assert.equal(M.view(q).current.reqId, "A");
});

test("skip undo restores the exact previous queue", () => {
  const q = M.createQueue(cards("A", "B", "C"));
  const result = M.skip(q);
  assert.equal(result.notice, "ข้ามไว้ท้ายคิว");
  assert.deepEqual(M.view(result.undo), M.view(q));
});

test("skipping the only card keeps it and explains why", () => {
  const q = M.createQueue(cards("A"));
  const result = M.skip(q);
  assert.equal(result.undo, null);
  assert.equal(result.notice, "มีใบนี้ใบเดียวที่รอ");
  assert.equal(M.view(result.state).current.reqId, "A");
});

test("deciding every card gives the empty state", () => {
  let q = M.createQueue(cards("A", "B"));
  q = M.decided(q, "A");
  q = M.decided(q, "B");
  assert.equal(M.view(q).mode, "empty");
});

test("refresh keeps the current card by REQ-id, keeps skips and appends new cards", () => {
  let q = M.createQueue(cards("A", "B", "C"));
  q = M.skip(q).state; // current B, A skipped
  q = M.refresh(q, cards("C", "B", "A", "D"));
  const v = M.view(q);
  assert.equal(v.current.reqId, "B");
  assert.equal(v.skippedCount, 1);
  assert.deepEqual(q.order, ["A", "B", "C", "D"]);
  q = M.refresh(q, cards("C", "D")); // B and A left the server queue
  assert.equal(M.view(q).current.reqId, "C");
  assert.equal(M.view(q).skippedCount, 0);
});

test("reason needs at least 10 characters after trim", () => {
  assert.equal(M.reasonStatus("   short   ").ok, false);
  assert.equal(M.reasonStatus("         a         ").remaining, 9);
  assert.equal(M.reasonStatus("เพิ่ม AC ข้อสอง").ok, true);
  assert.equal(M.reasonStatus("1234567890").label, "10/10");
});

test("approve is never enabled for incomplete cards, while busy or when GitHub is unusable", () => {
  const complete = { reqId: "A", incomplete: [] };
  assert.equal(M.actions(complete, {}).approve.enabled, true);
  const incomplete = M.actions({ reqId: "A", incomplete: ["Acceptance criteria"] }, {});
  assert.equal(incomplete.approve.enabled, false);
  assert.match(incomplete.reason, /ไม่มี Acceptance criteria/);
  assert.equal(incomplete.request.enabled, true);
  for (const blocked of ["github_unavailable", "rate_limited", "token_missing", "token_expired", "token_scope", "session_expired", "conflict", "network_unknown"]) {
    const state = M.actions(complete, { blocked });
    assert.equal(state.approve.enabled, false, blocked);
    assert.equal(state.request.enabled, false, blocked);
    assert.ok(state.reason.length > 0, `${blocked} needs a readable reason`);
  }
  assert.equal(M.actions(complete, { blocked: "github_unavailable" }).skip.enabled, true);
  assert.equal(M.actions(complete, { blocked: "token_missing" }).skip.enabled, false);
  assert.equal(M.actions(complete, { busy: "submit" }).approve.enabled, false);
  const stuck = M.actions({ reqId: "A", incomplete: [], pendingStatusRetry: { idempotencyKey: "k" } }, {});
  assert.equal(stuck.approve.enabled, false);
});

test("idempotency keys match the server pattern", () => {
  const key = M.newKey("approve");
  assert.match(key, /^[A-Za-z0-9_-]{12,128}$/);
  assert.notEqual(key, M.newKey("approve"));
});

const source = fs.readFileSync(path.join(__dirname, "..", "static", "review.js"), "utf8");
const fnBody = (name) => {
  const start = source.indexOf(`function ${name}(`);
  assert.ok(start >= 0, name);
  let depth = 0;
  for (let i = source.indexOf("{", start); i < source.length; i++) {
    if (source[i] === "{") depth++;
    if (source[i] === "}" && --depth === 0) return source.slice(start, i + 1);
  }
  throw new Error(name);
};

test("Skip and restart never call the network", () => {
  for (const name of ["skipCurrent", "restartQueue"]) {
    const body = fnBody(name);
    assert.doesNotMatch(body, /fetch\(|api\(|load\(/, name);
  }
});

test("success copy only appears after a 2xx server response (no optimistic UI)", () => {
  assert.match(fnBody("submitApprove"), /if \(status >= 200 && status < 300 && data\.ok\) return decisionDone/);
  assert.match(fnBody("submitRequest"), /if \(status >= 200 && status < 300 && data\.ok\)/);
  assert.doesNotMatch(fnBody("openApprove"), /decided\(|อนุมัติ .* แล้ว/);
  assert.match(fnBody("decisionDone"), /M\.decided\(app\.queue, card\.reqId\)/);
});

test("approve confirmation does not preselect the confirm button", () => {
  const body = fnBody("renderApprove");
  assert.match(body, /button\("ยกเลิก", \(\) => closeSheet\(\), "ghost", \{ "data-initial": true \}\)/);
});

test("drafts use sessionStorage only and the page never renders a token field", () => {
  assert.doesNotMatch(source, /localStorage/);
  const html = fs.readFileSync(path.join(__dirname, "..", "templates", "review.html"), "utf8");
  assert.doesNotMatch(html + source, /type="password"[^>]*token|github[_ ]?token"?\s*[:=]/i);
  assert.match(html, /ข้อมูลจำลอง — ยังไม่เชื่อม GitHub/);
  assert.doesNotMatch(html, /--label-quaternary\)/);
});

test("server HTML is the only innerHTML sink", () => {
  const sinks = source.match(/\.innerHTML\s*=/g) || [];
  assert.equal(sinks.length, 1);
  assert.match(source, /node\.innerHTML = markup; return node; \}; \/\/ markup is server-sanitized/);
});
