(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.VedaReviewModel = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  // UI interpretations from ux-designer that the user has not confirmed yet.
  // Flip here (the server sends the same values in queue.policy).
  const POLICY = {
    counterShrinksOnDecision: true, // decided cards leave the queue and the total shrinks
    skipUndoSeconds: 5,             // Skip shows a client-only undo toast
  };
  const MIN_REASON = 10;

  // ---------- session queue (client state per tab, design R4/R14.6) ----------
  function createQueue(cards) {
    return { order: cards.map((card) => card.reqId), cards: index(cards), cursor: 0, skipped: [], decided: [] };
  }

  function index(cards) {
    const map = {};
    for (const card of cards) map[card.reqId] = card;
    return map;
  }

  function clone(state) {
    return { order: state.order.slice(), cards: Object.assign({}, state.cards), cursor: state.cursor, skipped: state.skipped.slice(), decided: state.decided.slice() };
  }

  function currentId(state) {
    return state.cursor < state.order.length ? state.order[state.cursor] : null;
  }

  function view(state) {
    const total = POLICY.counterShrinksOnDecision ? state.order.length : state.order.length + state.decided.length;
    const id = currentId(state);
    if (state.order.length === 0) return { mode: "empty", current: null, position: 0, total, skippedCount: 0, onlyOne: false };
    if (!id) return { mode: "skipped-only", current: null, position: 0, total, skippedCount: state.skipped.length, skippedIds: state.skipped.slice() };
    const passed = POLICY.counterShrinksOnDecision ? state.cursor : state.cursor + state.decided.length;
    return { mode: "ready", current: state.cards[id], position: passed + 1, total, skippedCount: state.skipped.length, onlyOne: state.order.length === 1 };
  }

  // Invariant: skipped cards are exactly the cards before the cursor, so a
  // skipped card sits "behind" the reviewer until restart (REQ-009).

  // Skip never touches the network (REQ-009). Returns { state, undo, notice }.
  function skip(state) {
    const id = currentId(state);
    if (!id) return { state, undo: null, notice: null };
    if (state.order.length === 1) return { state, undo: null, notice: "มีใบนี้ใบเดียวที่รอ" };
    const next = clone(state);
    next.skipped.push(id);
    next.cursor += 1;
    return { state: next, undo: clone(state), notice: "ข้ามไว้ท้ายคิว" };
  }

  function restart(state) {
    const next = clone(state);
    const fresh = next.order.filter((id) => !next.skipped.includes(id));
    next.order = fresh.concat(next.skipped);
    next.skipped = [];
    next.cursor = 0;
    return next;
  }

  // Remove a card only after the server confirmed the decision (no optimistic UI).
  function decided(state, reqId) {
    const next = clone(state);
    const at = next.order.indexOf(reqId);
    if (at === -1) return next;
    next.order.splice(at, 1);
    next.skipped = next.skipped.filter((id) => id !== reqId);
    if (!next.decided.includes(reqId)) next.decided.push(reqId);
    delete next.cards[reqId];
    if (at < next.cursor) next.cursor -= 1;
    return next;
  }

  // Merge a fresh server list: keep session order, skip set and the current card by REQ-id.
  function refresh(state, cards) {
    const fresh = index(cards);
    const keepId = currentId(state);
    const skipped = state.skipped.filter((id) => fresh[id]);
    const rest = state.order.filter((id) => fresh[id] && !skipped.includes(id));
    const added = cards.map((card) => card.reqId).filter((id) => !state.order.includes(id));
    const order = skipped.concat(rest, added);
    let cursor = skipped.length;
    if (keepId && fresh[keepId] && !skipped.includes(keepId)) cursor = order.indexOf(keepId);
    return { order, cards: fresh, cursor, skipped, decided: state.decided.slice() };
  }

  // ---------- reason validation (REQ-008 a, design RequestChangesSheet) ----------
  function reasonStatus(text) {
    const count = Array.from(String(text || "").trim()).length;
    const remaining = Math.max(0, MIN_REASON - count);
    return { count, ok: remaining === 0, remaining, label: `${Math.min(count, 999)}/${MIN_REASON}`, helper: remaining ? `ใส่เหตุผลอีก ${remaining} ตัวอักษร` : "ครบแล้ว" };
  }

  // ---------- action availability (design R4 ActionBar, R6, R7) ----------
  const BLOCKING = {
    github_unavailable: { scope: "write", reason: "เชื่อมต่อ GitHub ไม่ได้ชั่วคราว — ยังไม่ได้เปลี่ยนอะไร" },
    rate_limited: { scope: "write", reason: "GitHub จำกัดจำนวนคำขออยู่ — รอจนถึงเวลาที่แจ้ง" },
    network_unknown: { scope: "write", reason: "ยังไม่ทราบผลการบันทึก — กด ตรวจสถานะ ก่อน" },
    token_missing: { scope: "all", reason: "ยังไม่ได้ตั้งค่า GitHub token" },
    token_expired: { scope: "all", reason: "GitHub token หมดอายุหรือถูกเพิกถอน" },
    token_scope: { scope: "all", reason: "GitHub token สิทธิ์ไม่พอ" },
    session_expired: { scope: "all", reason: "เซสชันหมดอายุ — เข้าสู่ระบบอีกครั้ง" },
    conflict: { scope: "write", reason: "ใบนี้เปลี่ยนไปแล้ว — โหลดฉบับล่าสุดก่อน" },
  };

  function actions(card, ctx) {
    const c = ctx || {};
    const off = (reason) => ({ enabled: false, reason });
    const on = { enabled: true, reason: "" };
    if (!card) return { approve: off(""), request: off(""), skip: off(""), reason: "" };
    if (c.busy) {
      const reason = c.busy === "submit" ? "กำลังบันทึก…" : "กำลังโหลดข้อมูลล่าสุด";
      return { approve: off(reason), request: off(reason), skip: off(reason), reason };
    }
    const block = c.blocked && BLOCKING[c.blocked];
    if (block) {
      const skipState = block.scope === "all" ? off(block.reason) : on;
      return { approve: off(block.reason), request: off(block.reason), skip: skipState, reason: block.reason };
    }
    if (card.pendingStatusRetry) {
      const reason = `${card.reqId} ยังไม่ได้ตั้งสถานะ Changes requested — ลองอีกครั้ง`;
      return { approve: off(reason), request: on, skip: on, reason };
    }
    if (card.incomplete && card.incomplete.length) {
      const reason = `ใบนี้ข้อมูลไม่ครบ (ไม่มี ${card.incomplete.join(", ")}) — ส่งกลับให้ ba เติม`;
      return { approve: off(reason), request: on, skip: on, reason };
    }
    return { approve: on, request: on, skip: on, reason: "" };
  }

  function relativeTime(iso, now) {
    const then = Date.parse(iso || "");
    if (!Number.isFinite(then)) return "ไม่ระบุ";
    const minutes = Math.max(0, Math.round(((now || Date.now()) - then) / 60000));
    if (minutes < 1) return "เมื่อสักครู่";
    if (minutes < 60) return `${minutes} นาทีที่แล้ว`;
    const hours = Math.round(minutes / 60);
    if (hours < 24) return `${hours} ชม.ที่แล้ว`;
    return `${Math.round(hours / 24)} วันที่แล้ว`;
  }

  function newKey(prefix) {
    const bytes = new Uint8Array(12);
    (globalThis.crypto || require("node:crypto").webcrypto).getRandomValues(bytes);
    return `${prefix}_${Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("")}`;
  }

  return { POLICY, MIN_REASON, createQueue, view, skip, restart, decided, refresh, reasonStatus, actions, relativeTime, newKey, currentId };
});
