/* Requirement Review Queue page (/review). Design: .veda/projects/app-dashboard/design.md R1-R15.
   Rules: no optimistic UI, Skip never calls the network, server HTML is pre-sanitized,
   everything else is inserted with textContent. */
(function () {
  "use strict";
  const M = window.VedaReviewModel;
  const $ = (id) => document.getElementById(id);
  const el = (tag, attrs, children) => {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? "" : value);
    }
    for (const child of [].concat(children || [])) if (child) node.append(child.nodeType ? child : document.createTextNode(String(child)));
    return node;
  };
  const ICON = {
    err: '<svg class="ico" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"><path d="M10 1a9 9 0 1 0 0 18 9 9 0 0 0 0-18Zm-1 4h2v6H9V5Zm0 8h2v2H9v-2Z"/></svg>',
    warn: '<svg class="ico" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"><path d="M10 2 1 18h18L10 2Zm-1 6h2v5H9V8Zm0 6h2v2H9v-2Z"/></svg>',
    info: '<svg class="ico" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"><path d="M10 1a9 9 0 1 0 0 18 9 9 0 0 0 0-18Zm-1 4h2v2H9V5Zm0 4h2v6H9V9Z"/></svg>',
    ok: '<svg class="ico" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"><path d="M10 1a9 9 0 1 0 0 18 9 9 0 0 0 0-18Zm-1.2 12.6L4.6 9.4l1.4-1.4 2.8 2.8 5.2-5.2 1.4 1.4-6.6 6.6Z"/></svg>',
  };
  ICON.conflict = ICON.warn;

  const app = {
    csrf: "", queue: null, data: null, blocked: null, retryAt: null, busy: null, sheet: null,
    loggedIn: false, reviewer: null, mode: null, opener: null, updated: new Set(), lastDecision: null,
    readToEnd: new Set(), observer: null, firstLoad: true,
  };

  // ---------- network ----------
  async function api(path, options) {
    const opts = Object.assign({ method: "GET", headers: {} }, options || {});
    opts.credentials = "same-origin";
    if (opts.method !== "GET") {
      opts.headers["Content-Type"] = "application/json";
      opts.headers["X-CSRF-Token"] = app.csrf;
      if (opts.key) opts.headers["Idempotency-Key"] = opts.key;
      opts.body = JSON.stringify(opts.body || {});
    }
    let response;
    try {
      response = await fetch(path, opts);
    } catch (_) {
      return { status: 0, data: { ok: false, code: "network_unknown", error: "เชื่อมต่อ dashboard ไม่ได้" } };
    }
    let data = {};
    try { data = await response.json(); } catch (_) { data = { ok: false, code: "network_unknown", error: "คำตอบจาก server อ่านไม่ได้" }; }
    return { status: response.status, data };
  }

  async function ensureSession() {
    const { data } = await api("/api/session");
    app.loggedIn = Boolean(data && data.authenticated);
    app.csrf = (data && data.csrfToken) || "";
    return app.loggedIn;
  }

  // ---------- loading ----------
  async function load(opts) {
    const quiet = opts && opts.quiet;
    app.busy = app.queue ? "refresh" : null;
    $("refreshBtn").setAttribute("aria-busy", "true");
    if (app.queue) { const card = document.querySelector(".card"); if (card) card.classList.add("fading"); renderBar(); }
    const { status, data } = await api("/api/review/queue");
    $("refreshBtn").removeAttribute("aria-busy");
    app.busy = null;
    $("stage").setAttribute("aria-busy", "false");
    if (data.mode) setMode(data.mode, data.debugAvailable);
    if (data.reviewer) setReviewer(data.reviewer);
    if (status === 401) return showLogin(data.code === "session_expired");
    if (status === 403) return showForbidden();
    if (!data.ok) {
      app.blocked = data.code || "github_unavailable";
      app.retryAt = data.retryAt || null;
      app.data = Object.assign({}, app.data || {}, { error: data });
      return render();
    }
    app.blocked = null; app.retryAt = null;
    app.data = data;
    const before = app.queue && M.currentId(app.queue) ? app.queue.cards[M.currentId(app.queue)] : null;
    app.queue = app.queue ? M.refresh(app.queue, data.cards) : M.createQueue(data.cards);
    const after = before && app.queue.cards[before.reqId];
    if (after && after.hash !== before.hash) app.updated.add(before.reqId);
    render();
    if (!quiet && app.firstLoad) { app.firstLoad = false; focusTitle(false); }
  }

  function setMode(mode, debugAvailable) {
    app.mode = mode;
    $("mockBanner").classList.toggle("hidden", mode !== "mock");
    if (mode === "mock" && debugAvailable && app.reviewer && app.reviewer.role === "admin") loadDebug();
  }

  function setReviewer(reviewer) {
    app.reviewer = reviewer;
    const badge = $("reviewerBadge");
    badge.textContent = `${reviewer.username} · ${reviewer.role === "admin" ? "Admin" : reviewer.role}`;
    badge.classList.remove("hidden");
  }

  // ---------- rendering ----------
  function notice(kind, title, text, buttons, role) {
    const box = el("div", { class: `notice ${kind}`, role: role || (kind === "err" || kind === "conflict" ? "alert" : "status") });
    box.insertAdjacentHTML("afterbegin", ICON[kind] || ICON.info);
    const body = el("div", { class: "body" }, [el("h2", { text: title }), text ? el("p", { text }) : null]);
    if (buttons && buttons.length) body.append(el("div", { class: "notice-actions" }, buttons));
    box.append(body);
    return box;
  }
  const button = (label, onclick, cls, extra) => el("button", Object.assign({ type: "button", class: `btn ${cls || "ghost"}`, onclick }, extra || {}), label);
  const backButton = () => el("a", { class: "btn ghost", href: "/" }, "กลับ Dashboard");

  function minutesUntil(iso) {
    const ms = Date.parse(iso || "") - Date.now();
    return Number.isFinite(ms) ? Math.max(0, Math.ceil(ms / 60000)) : null;
  }
  const clock = (iso) => { const d = new Date(iso); return Number.isFinite(d.getTime()) ? d.toLocaleTimeString("th-TH", { hour: "2-digit", minute: "2-digit" }) : ""; };

  function blockedNotice() {
    const code = app.blocked;
    if (!code) return null;
    const retry = () => load();
    switch (code) {
      case "github_unavailable":
        return notice("warn", "เชื่อมต่อ GitHub ไม่ได้ชั่วคราว", "ยังไม่ได้เปลี่ยนอะไรใน requirement นี้ ลองอีกครั้งในอีกสักครู่", [button("ลองอีกครั้ง", retry, "primary")]);
      case "rate_limited": {
        const minutes = minutesUntil(app.retryAt);
        const until = Date.parse(app.retryAt || "");
        const btn = button("ลองอีกครั้ง", retry, "primary", { disabled: Number.isFinite(until) && until > Date.now() });
        if (Number.isFinite(until) && until > Date.now()) setTimeout(() => { btn.disabled = false; }, Math.min(until - Date.now(), 2 ** 31 - 1));
        return notice("warn", "GitHub จำกัดจำนวนคำขอ", minutes !== null ? `ลองได้อีกครั้งอีก ${minutes} นาที (${clock(app.retryAt)}) ระบบจะไม่ลองใหม่เอง` : "รอสักครู่แล้วลองใหม่", [btn]);
      }
      case "token_missing":
        return notice("err", "ยังไม่ได้ตั้งค่า GitHub token", "ตั้งใน .env.runtime ของ dashboard (ดูเอกสาร README หัวข้อ Requirement review) — หน้านี้ไม่มีช่องกรอก token", [backButton()]);
      case "token_expired":
        return notice("err", "GitHub token หมดอายุหรือถูกเพิกถอน", "สร้าง token ใหม่ตามขั้นตอนหมุน token ใน README แล้วรีสตาร์ต dashboard", [button("ตรวจอีกครั้ง", retry)]);
      case "token_scope": {
        const missing = app.data && app.data.error && app.data.error.details && app.data.error.details.missingScopes;
        return notice("err", "GitHub token สิทธิ์ไม่พอ", missing ? `ขาด scope: ${missing.join(", ")}` : "ตรวจ scope ของ token ตาม README", [button("ตรวจอีกครั้ง", retry)]);
      }
      default:
        return notice("err", "โหลดคิวรีวิวไม่สำเร็จ", (app.data && app.data.error && app.data.error.error) || "ลองอีกครั้ง", [button("ลองอีกครั้ง", retry, "primary")]);
    }
  }

  function render() {
    const notices = $("notices"); notices.replaceChildren();
    const stage = $("stage");
    const data = app.data || {};
    for (const warning of data.warnings || []) {
      if (warning.code === "token_expiring" && !sessionGet("dismiss-token-warning")) {
        notices.append(notice("warn", `GitHub token จะหมดอายุใน ${warning.days} วัน`, "หมุน token ตาม README ก่อนวันหมดอายุ", [button("ปิด", () => { sessionSet("dismiss-token-warning", "1"); render(); })], "status"));
      }
    }
    const blocked = blockedNotice();
    if (!app.queue) {
      $("progress").classList.add("hidden"); $("actionBar").classList.add("hidden");
      stage.replaceChildren(blocked || el("div"));
      return;
    }
    if (blocked) notices.append(blocked);
    if (app.reconcileError) {
      notices.append(notice("warn", `อนุมัติแล้ว แต่ยังไม่ได้ปลดล็อก ${app.reconcileError.project}`, "ระบบจะซ่อมเมื่อเปิดหน้านี้ใหม่", [button("ซ่อมตอนนี้", repairGate)]));
    }
    const v = M.view(app.queue);
    renderProgress(v);
    if (v.mode === "empty") {
      stage.replaceChildren(el("section", { class: "glass state-panel" }, [
        notice("ok", "ไม่มี requirement รอรีวิว", `เมื่อ ba ส่งใบใหม่ จะขึ้นที่นี่ — ตรวจล่าสุด ${clock(data.checkedAt)}`, [button("รีเฟรช", () => load(), "primary"), backButton()]),
        gateSummary(),
      ]));
      $("actionBar").classList.add("hidden");
      return;
    }
    if (v.mode === "skipped-only") {
      stage.replaceChildren(el("section", { class: "glass state-panel" }, [
        notice("info", `เหลือใบที่ข้ามไว้ ${v.skippedCount} ใบ`, "ใบที่ข้ามยังรอรีวิวอยู่ สถานะไม่เปลี่ยน", [button("เริ่มใหม่", restartQueue, "primary"), backButton()]),
        el("ul", {}, v.skippedIds.map((id) => el("li", { text: id }))),
        gateSummary(),
      ]));
      $("actionBar").classList.add("hidden");
      return;
    }
    const existing = stage.querySelector(".card");
    const sameCard = existing && existing.dataset.reqId === v.current.reqId && existing.dataset.hash === v.current.hash;
    if (!sameCard) {
      stage.replaceChildren(renderCard(v.current));
    } else {
      existing.classList.remove("fading");
    }
    renderBar();
  }

  function gateSummary() {
    const gate = (app.data && app.data.gate) || {};
    const lines = Object.values(gate).map((g) => el("li", { text: g.message || `${g.project}: requirementsReady=${g.requirementsReady}` }));
    if (!lines.length) return null;
    return el("div", { class: "section" }, [el("p", { class: "section-label", text: app.mode === "mock" ? "ผล gate (จำลอง)" : "ผล gate" }), el("ul", { class: "scope-list" }, lines)]);
  }

  function renderProgress(v) {
    const progress = $("progress");
    if (v.mode !== "ready") { progress.classList.add("hidden"); return; }
    progress.classList.remove("hidden");
    $("progressCount").textContent = `${v.position} จาก ${v.total}`;
    const seg = $("segments"); seg.replaceChildren();
    const count = Math.min(v.total, 24);
    for (let i = 0; i < count; i++) seg.append(el("span", { class: i < Math.round(((v.position - 1) / v.total) * count) ? "done" : "" }));
    seg.setAttribute("aria-valuemax", String(v.total)); seg.setAttribute("aria-valuenow", String(v.position));
    seg.setAttribute("aria-label", `ใบที่ ${v.position} จาก ${v.total}`);
    const chip = $("skippedChip");
    chip.classList.toggle("hidden", !v.skippedCount);
    chip.textContent = `ข้ามไว้ ${v.skippedCount} ใบ`;
  }

  const notSpecified = () => el("p", { class: "empty-value", text: "ไม่ระบุ" });
  const html = (cls, markup) => { const node = el("div", { class: cls }); node.innerHTML = markup; return node; }; // markup is server-sanitized

  function renderCard(card) {
    const requested = card.requestedAt ? el("time", { datetime: card.requestedAt, title: new Date(card.requestedAt).toLocaleString("th-TH"), "aria-label": `ขอรีวิวเมื่อ ${new Date(card.requestedAt).toLocaleString("th-TH")}` }, `ขอรีวิว ${M.relativeTime(card.requestedAt)}`) : el("span", { text: "ขอรีวิว: ไม่ระบุ" });
    const node = el("article", { class: "glass card enter", "data-req-id": card.reqId, "data-hash": card.hash, "aria-labelledby": "cardTitle" });
    if (app.updated.has(card.reqId)) node.append(el("div", { class: "updated-banner", role: "status", text: "อัปเดตแล้ว — ฉบับนี้เพิ่งเปลี่ยน" }));
    if (card.pendingStatusRetry) {
      node.append(notice("conflict", `${card.reqId} ยังไม่ได้ตั้งสถานะ Changes requested`, "คอมเมนต์ถูกโพสต์แล้ว — ลองตั้งสถานะอีกครั้งโดยไม่โพสต์ซ้ำ", [button("ลองอีกครั้ง", () => openRequest({ retryKey: card.pendingStatusRetry.idempotencyKey }), "request")]));
    }
    node.append(el("div", { class: "meta" }, [el("span", { class: "pill", lang: "en", text: card.reqId }), el("span", { class: "pill", lang: "en", text: card.project || "ไม่ระบุ" }), requested]));
    node.append(el("h1", { id: "cardTitle", tabindex: "-1", text: card.title || "ไม่ระบุ" }));
    if (card.history && card.history.length) {
      const box = el("section", { class: "history", "aria-label": "ประวัติการขอแก้ไข" });
      box.insertAdjacentHTML("beforeend", "");
      const heading = el("h2", {}, [`เคยขอแก้ไข (${card.history.length} ครั้ง)`]);
      heading.insertAdjacentHTML("afterbegin", '<svg width="16" height="16" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"><path d="M10 2 1 18h18L10 2Zm-1 6h2v5H9V8Zm0 6h2v2H9v-2Z"/></svg>');
      box.append(heading);
      for (const item of card.history) {
        const row = el("div", { class: "history-item" });
        row.append(el("div", { class: "when" }, [new Date(item.at).toLocaleString("th-TH"), ` · @${item.actor}`, item.contentChangedSinceRound ? el("span", { class: "tag", text: "เนื้อหาเปลี่ยนตั้งแต่รอบนั้น" }) : null]));
        const reason = el("div", { class: "reason", text: item.reason || "ไม่ระบุ" });
        row.append(reason);
        if ((item.reason || "").length > 160) row.append(el("button", { type: "button", class: "link-btn", onclick: (e) => { row.classList.toggle("open"); e.target.textContent = row.classList.contains("open") ? "ย่อ" : "ดูทั้งหมด"; } }, "ดูทั้งหมด"));
        box.append(row);
      }
      node.append(box);
    }
    const content = el("section", { class: "section", "aria-label": "เนื้อหา" });
    content.append(card.contentHtml ? html("prose", card.contentHtml) : notSpecified());
    if (card.contentSanitized) content.append(el("p", { class: "sanitized-note", text: "บางส่วนถูกตัดเพื่อความปลอดภัย" }));
    node.append(content);

    const scope = el("section", { class: "section" }, [el("h2", { class: "section-label", text: "Scope" })]);
    if (card.scope && card.scope.source === "feature") scope.append(el("p", { class: "scope-src", text: "ขอบเขตระดับฟีเจอร์ (ใช้ร่วมทุกใบของฟีเจอร์นี้)" }));
    const scopeList = (label, items, mark) => [el("p", { class: "scope-sub", text: label }), items && items.length ? el("ul", { class: "scope-list" }, items.map((item) => el("li", {}, [el("span", { class: "mark", "aria-hidden": "true", text: mark }), html("prose", item)]))) : notSpecified()];
    scope.append(...scopeList("อยู่ในขอบเขต", card.scope && card.scope.in, "✓"), ...scopeList("ไม่อยู่ในขอบเขต", card.scope && card.scope.out, "–"));
    node.append(scope);

    const ac = el("section", { class: "section", id: "acSection" }, [el("h2", { class: "section-label", text: "Acceptance criteria" })]);
    ac.append(card.acceptanceCriteria && card.acceptanceCriteria.length ? el("ol", { class: "ac-list" }, card.acceptanceCriteria.map((item) => el("li", {}, [html("prose", item)]))) : notSpecified());
    node.append(ac);

    const links = el("div", { class: "links" });
    if (card.issue && card.issue.url) links.append(el("a", { href: card.issue.url, target: "_blank", rel: "noopener noreferrer" }, "เปิด Issue ↗"));
    else if (card.issue && card.issue.number) links.append(el("span", { class: "mock-link", text: `Issue จำลอง #${card.issue.number} — ยังไม่ได้สร้างบน GitHub` }));
    else links.append(notSpecified());
    if (card.prUrl) links.append(el("a", { href: card.prUrl, target: "_blank", rel: "noopener noreferrer" }, "PR ↗"));
    node.append(el("section", { class: "section", "aria-label": "ลิงก์" }, [links]));

    const approvals = el("div", { class: "approvals", "aria-label": "ผู้อนุมัติ" }, (card.approvals || []).map((a) => el("span", { class: "chip", text: `@${a.actor} ✓` })));
    const countLine = el("span", { class: card.requiredApprovals <= 1 && !card.approvedCount ? "hidden" : "", text: `อนุมัติแล้ว ${card.approvedCount} จาก ${card.requiredApprovals}` });
    node.append(el("footer", { class: "footer-meta" }, [el("span", { lang: "en", text: `hash ${card.shortHash || "ไม่ระบุ"}` }), approvals, countLine]));
    node.append(el("div", { id: "cardEnd", "aria-hidden": "true" }));
    observeEnd(card.reqId, node);
    return node;
  }

  function observeEnd(reqId, node) {
    if (app.observer) app.observer.disconnect();
    if (!("IntersectionObserver" in window)) { app.readToEnd.add(reqId); return; }
    app.observer = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) { app.readToEnd.add(reqId); $("barHint").classList.add("hidden"); }
    });
    requestAnimationFrame(() => { const end = node.querySelector("#cardEnd"); if (end) app.observer.observe(end); });
  }

  function renderBar() {
    const v = app.queue ? M.view(app.queue) : null;
    const bar = $("actionBar");
    if (!v || v.mode !== "ready" || ["token_missing"].includes(app.blocked)) { bar.classList.add("hidden"); return; }
    bar.classList.remove("hidden");
    const state = M.actions(v.current, { busy: app.busy, blocked: app.blocked });
    for (const [id, key] of [["approveBtn", "approve"], ["requestBtn", "request"], ["skipBtn", "skip"]]) {
      const btn = $(id);
      btn.disabled = !state[key].enabled;
      if (!state[key].enabled && state.reason) btn.setAttribute("aria-describedby", "barReason"); else btn.removeAttribute("aria-describedby");
    }
    const reason = $("barReason");
    reason.textContent = state.reason; reason.classList.toggle("hidden", !state.reason);
    $("barHint").classList.toggle("hidden", app.readToEnd.has(v.current.reqId));
    requestAnimationFrame(() => document.documentElement.style.setProperty("--bar-h", `${bar.offsetHeight}px`));
  }

  function focusTitle(announce) {
    window.scrollTo({ top: 0, behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
    const title = $("cardTitle");
    if (title) title.focus({ preventScroll: true });
    if (announce) {
      const v = M.view(app.queue);
      if (v.mode === "ready") $("live").textContent = `ใบที่ ${v.position} จาก ${v.total} ${v.current.reqId}`;
    }
  }

  // ---------- toasts ----------
  function toast(message, actionLabel, onAction, seconds) {
    const node = el("div", { class: "toast", role: "status" }, [el("span", { text: message })]);
    let timer;
    const close = () => { clearTimeout(timer); node.remove(); };
    if (actionLabel) node.append(button(actionLabel, () => { close(); onAction(); }, "primary"));
    $("toasts").append(node);
    $("live").textContent = message;
    timer = setTimeout(close, (seconds || 4) * 1000);
    return close;
  }

  // ---------- skip (client only, REQ-009) ----------
  function skipCurrent() {
    const result = M.skip(app.queue);
    if (result.notice && !result.undo) { toast(result.notice); return; }
    app.queue = result.state;
    render(); focusTitle(true);
    const undo = result.undo;
    toast("ข้ามไว้ท้ายคิว", "เลิกทำ", () => { app.queue = undo; render(); focusTitle(true); }, M.POLICY.skipUndoSeconds);
  }
  function restartQueue() { app.queue = M.restart(app.queue); render(); focusTitle(true); }

  // ---------- sheet ----------
  function openSheet(build, opener) {
    app.opener = opener || document.activeElement;
    $("scrim").classList.remove("hidden");
    const sheet = $("sheet");
    sheet.classList.remove("hidden");
    build(sheet);
    document.body.style.overflow = "hidden";
    const initial = sheet.querySelector("[data-initial]") || sheet.querySelector("button, textarea");
    if (initial) initial.focus();
  }
  function closeSheet(force) {
    if (app.sheet && app.sheet.pending && !force) return;
    app.sheet = null;
    $("sheet").classList.add("hidden"); $("sheet").replaceChildren();
    $("scrim").classList.add("hidden");
    document.body.style.overflow = "";
    if (app.opener && document.contains(app.opener)) app.opener.focus();
  }
  function sheetFrame(sheet, title, bodyNodes, footNodes) {
    sheet.replaceChildren(
      el("div", { class: "sheet-body" }, [el("h2", { id: "sheetTitle", tabindex: "-1", text: title }), ...bodyNodes]),
      el("div", { class: "sheet-foot" }, footNodes),
    );
  }
  function trapFocus(event) {
    const sheet = $("sheet");
    if (sheet.classList.contains("hidden")) return;
    if (event.key === "Escape") { event.preventDefault(); closeSheet(); return; }
    if (event.key !== "Tab") return;
    const items = Array.from(sheet.querySelectorAll("button:not(:disabled), textarea, a[href], [tabindex='0']"));
    if (!items.length) return;
    const first = items[0], last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }

  const spinnerLabel = (text) => [el("span", { class: "spinner", "aria-hidden": "true" }), text];
  const current = () => { const v = M.view(app.queue); return v.mode === "ready" ? v.current : null; };

  // ---------- approve (REQ-007) ----------
  function openApprove(opts) {
    const card = current();
    if (!card || $("approveBtn").disabled) return;
    const key = (opts && opts.key) || M.newKey("approve");
    app.sheet = { kind: "approve", reqId: card.reqId, key, pending: false };
    openSheet((sheet) => renderApprove(sheet, card, null), $("approveBtn"));
  }
  function renderApprove(sheet, card, error) {
    const confirm = button("ยืนยันอนุมัติ", () => submitApprove(card), "approve");
    if (error) confirm.textContent = "ลองอีกครั้ง";
    const body = [
      el("p", { class: "sub" }, [el("span", { class: "mono", lang: "en", text: card.reqId }), ` · ${card.title}`]),
      el("p", { class: "sub mono", lang: "en", text: `hash ${card.shortHash}` }),
    ];
    if (card.isLastOfProject) body.push(el("p", { class: "note", text: `ใบสุดท้ายของ ${card.project} — อนุมัติแล้ว requirementsReady จะเป็น true${app.mode === "mock" ? " (โหมดจำลอง: บันทึกใน sandbox เท่านั้น)" : ""}` }));
    if (error) body.push(el("div", { class: "inline-error", role: "alert", text: error }));
    sheetFrame(sheet, "อนุมัติ requirement นี้?", body, [button("ยกเลิก", () => closeSheet(), "ghost", { "data-initial": true }), confirm]);
  }
  async function submitApprove(card) {
    const sheet = $("sheet");
    app.sheet.pending = true;
    const confirm = sheet.querySelector(".btn.approve");
    const cancel = sheet.querySelector(".btn.ghost");
    confirm.disabled = true; cancel.disabled = true; confirm.replaceChildren(...spinnerLabel("กำลังบันทึกใน GitHub…"));
    const { status, data } = await api(`/api/review/cards/${encodeURIComponent(card.reqId)}/approve`, { method: "POST", key: app.sheet.key, body: { hash: card.hash, sectionHashes: card.sectionHashes } });
    app.sheet.pending = false;
    if (status >= 200 && status < 300 && data.ok) return decisionDone(card, "approve", data);
    handleDecisionError(card, "approve", status, data);
  }

  // ---------- request changes (REQ-008) ----------
  const draftKey = (reqId) => `review-draft:${reqId}`;
  function openRequest(opts) {
    const card = current();
    if (!card) return;
    const retryKey = opts && opts.retryKey;
    if (!retryKey && $("requestBtn").disabled) return;
    const key = retryKey || (app.sheet && app.sheet.kind === "request" && app.sheet.reqId === card.reqId && app.sheet.key) || M.newKey("changes");
    app.sheet = { kind: "request", reqId: card.reqId, key, pending: false, twoPhase: Boolean(retryKey) };
    openSheet((sheet) => (retryKey ? renderTwoPhase(sheet, card, null) : renderRequest(sheet, card, null)), $("requestBtn"));
  }
  function renderRequest(sheet, card, error) {
    const draft = readDraft(card.reqId);
    const area = el("textarea", { id: "reasonInput", rows: "4", autocapitalize: "sentences", "aria-describedby": "reasonHelp reasonNote" });
    area.value = draft ? draft.text : "";
    const counter = el("span", { id: "reasonCount" });
    const help = el("span", { id: "reasonHelp" });
    const submit = button("ส่งกลับให้แก้", () => {
      const status = M.reasonStatus(area.value);
      if (!status.ok) { area.setAttribute("aria-invalid", "true"); help.className = "err"; help.textContent = `ต้องใส่เหตุผลอย่างน้อย ${M.MIN_REASON} ตัวอักษร (ขาดอีก ${status.remaining})`; area.focus(); return; }
      submitRequest(card, area.value);
    }, "request");
    const update = () => {
      const status = M.reasonStatus(area.value);
      counter.textContent = status.ok ? `${status.label} ✓` : status.label;
      counter.className = status.ok ? "okmark" : "";
      if (help.className !== "err" || status.ok) { help.className = ""; help.textContent = status.ok ? "" : status.helper; }
      if (status.ok) area.removeAttribute("aria-invalid");
      submit.disabled = !status.ok;
      area.style.height = "auto"; area.style.height = `${Math.min(area.scrollHeight, 220)}px`;
      writeDraft(card.reqId, area.value, draft && draft.hash !== card.hash ? draft.hash : card.hash);
    };
    area.addEventListener("input", update);
    area.addEventListener("focus", () => setTimeout(() => area.scrollIntoView({ block: "center" }), 250));
    const body = [
      el("p", { class: "sub" }, [el("span", { class: "mono", lang: "en", text: card.reqId }), ` · ${card.title}`]),
      el("label", { for: "reasonInput", text: `เหตุผล (อย่างน้อย ${M.MIN_REASON} ตัวอักษร)` }),
      area,
      el("div", { class: "counter-row" }, [help, counter]),
      draft && draft.hash && draft.hash !== card.hash ? el("span", { class: "draft-tag", text: "ร่างจากฉบับก่อนหน้า" }) : null,
      el("p", { id: "reasonNote", class: "note", text: "เหตุผลจะถูกโพสต์เป็นคอมเมนต์บน issue ให้ ba อ่าน" }),
      error ? el("div", { class: "inline-error", role: "alert", text: error }) : null,
    ].filter(Boolean);
    sheetFrame(sheet, "Request changes", body, [button("ยกเลิก", () => closeSheet(), "ghost"), submit]);
    update();
    area.setAttribute("data-initial", "");
  }
  function renderTwoPhase(sheet, card, error) {
    const retry = button("ลองตั้งสถานะอีกครั้ง", () => submitRequest(card, ""), "request", { "data-initial": true });
    sheetFrame(sheet, "คอมเมนต์โพสต์แล้ว แต่ยังตั้งสถานะไม่สำเร็จ", [
      el("p", { class: "sub" }, [el("span", { class: "mono", lang: "en", text: card.reqId }), ` · ${card.title}`]),
      el("p", { class: "note", text: "เหตุผลถูกโพสต์บน issue แล้ว — การลองใหม่จะตั้งสถานะ Changes requested อย่างเดียว ไม่โพสต์คอมเมนต์ซ้ำ" }),
      error ? el("div", { class: "inline-error", role: "alert", text: error }) : null,
    ].filter(Boolean), [button("ปิด", () => closeSheet(), "ghost"), retry]);
  }
  async function submitRequest(card, reason) {
    const sheet = $("sheet");
    app.sheet.pending = true;
    const submit = sheet.querySelector(".btn.request");
    sheet.querySelectorAll("button, textarea").forEach((n) => { n.disabled = true; });
    submit.replaceChildren(...spinnerLabel("กำลังบันทึกใน GitHub…"));
    const { status, data } = await api(`/api/review/cards/${encodeURIComponent(card.reqId)}/request-changes`, { method: "POST", key: app.sheet.key, body: { hash: card.hash, sectionHashes: card.sectionHashes, reason } });
    app.sheet.pending = false;
    if (status >= 200 && status < 300 && data.ok) { clearDraft(card.reqId); return decisionDone(card, "request", data); }
    if (data.code === "status_failed") {
      app.sheet.twoPhase = true;
      card.pendingStatusRetry = { idempotencyKey: app.sheet.key, commentId: data.commentId };
      clearDraft(card.reqId);
      return renderTwoPhase(sheet, card, "GitHub ยังไม่ยอมรับการตั้งสถานะ — ลองอีกครั้งได้โดยไม่โพสต์ซ้ำ");
    }
    handleDecisionError(card, "request", status, data);
  }

  // ---------- decision outcomes ----------
  function decisionDone(card, kind, data) {
    closeSheet(true);
    app.updated.delete(card.reqId);
    app.queue = M.decided(app.queue, card.reqId);
    const reconcile = data.reconcile || {};
    app.reconcileError = reconcile.error ? reconcile : null;
    toast(kind === "approve" ? `อนุมัติ ${card.reqId} แล้ว` : `ส่งกลับ ${card.reqId} ให้แก้แล้ว`);
    if (kind === "approve" && reconcile.requirementsReady) toast(app.mode === "mock" ? `จำลอง: จะตั้ง requirementsReady ของ ${reconcile.project} เป็น true (ยังไม่เขียน registry)` : `requirementsReady ของ ${reconcile.project} เป็น true แล้ว`, null, null, 6);
    if (data.warning) toast(data.warning, null, null, 6);
    if (app.data && reconcile.project) app.data.gate = Object.assign({}, app.data.gate, { [reconcile.project]: reconcile });
    render(); focusTitle(true);
    load({ quiet: true });
  }

  function handleDecisionError(card, kind, status, data) {
    const sheet = $("sheet");
    const rerender = (message) => (kind === "approve" ? renderApprove(sheet, card, message) : (app.sheet.twoPhase ? renderTwoPhase(sheet, card, message) : renderRequest(sheet, card, message)));
    if (status === 401) {
      return sheetFrame(sheet, "เซสชันหมดอายุ", [el("p", { class: "note", role: "alert", text: "ยังไม่ได้บันทึกการตัดสินนี้ ร่างเหตุผลยังเก็บไว้ เข้าสู่ระบบแล้วกดยืนยันใหม่อีกครั้ง (ระบบจะไม่ส่งซ้ำเอง)" })],
        [button("เข้าสู่ระบบอีกครั้ง", () => { closeSheet(true); showLogin(true); }, "primary", { "data-initial": true })]);
    }
    if (data.code === "conflict_hash" || data.code === "conflict_state") return renderConflict(sheet, card, data);
    if (data.code === "network_unknown" || status === 0) return renderUnknown(sheet, card, kind);
    if (data.code === "reason_too_short") return rerender(data.error);
    if (status === 403) return rerender(data.code === "csrf_invalid" ? "CSRF token ไม่ถูกต้อง — รีเฟรชหน้าแล้วลองใหม่" : "บัญชีนี้ไม่มีสิทธิ์รีวิว requirement");
    if (data.code === "rate_limited") { app.blocked = "rate_limited"; app.retryAt = data.retryAt; return rerender(`GitHub จำกัดจำนวนคำขอ — ลองได้อีกครั้งอีก ${minutesUntil(data.retryAt)} นาที ยังไม่ได้เปลี่ยนอะไร`); }
    if (["token_missing", "token_expired", "token_scope"].includes(data.code)) { app.blocked = data.code; closeSheet(true); return render(); }
    if (data.code === "card_incomplete") return rerender(data.error);
    rerender(`${data.error || "บันทึกไม่สำเร็จ"} — สถานะยังไม่เปลี่ยน ลองอีกครั้งได้`);
  }

  function renderConflict(sheet, card, data) {
    const stateChanged = data.code === "conflict_state";
    const body = stateChanged
      ? [el("p", { class: "note", text: `ใบนี้ไม่ได้อยู่ในสถานะรอรีวิวแล้ว (${data.currentStatus || "ไม่ทราบ"})` })]
      : [el("p", { class: "note", text: "ยังไม่ได้อนุมัติอะไร โปรดอ่านฉบับล่าสุดก่อนตัดสินใหม่" }),
         el("p", { class: "sub mono", lang: "en", text: `${data.oldHash || card.shortHash} → ${data.newHash || "?"}` }),
         data.changedSections && data.changedSections.length ? el("p", { class: "sub", text: `${data.changedSections.join(", ")} เปลี่ยน` }) : null].filter(Boolean);
    sheet.replaceChildren(el("div", { class: "sheet-body", role: "alert" }, [notice("conflict", "requirement นี้เปลี่ยนไปขณะที่คุณอ่าน", "", [], "alert"), ...body]),
      el("div", { class: "sheet-foot" }, [stateChanged
        ? button("ไปใบถัดไป", () => { closeSheet(true); app.queue = M.decided(app.queue, card.reqId); render(); focusTitle(true); }, "primary", { "data-initial": true })
        : button("โหลดฉบับล่าสุด", () => reloadCard(card.reqId), "primary", { "data-initial": true })]));
    const first = sheet.querySelector("[data-initial]"); if (first) first.focus();
  }

  async function reloadCard(reqId) {
    const { status, data } = await api(`/api/review/cards/${encodeURIComponent(reqId)}`);
    closeSheet(true);
    if (status === 200 && data.ok) {
      if (data.card.status !== "Needs review") { app.queue = M.decided(app.queue, reqId); render(); focusTitle(true); toast(`${reqId} ไม่ได้รอรีวิวแล้ว (${data.card.status})`); return; }
      app.queue.cards[reqId] = data.card;
      app.updated.add(reqId);
      $("stage").replaceChildren();
      render(); focusTitle(false);
      return;
    }
    load();
  }

  function renderUnknown(sheet, card, kind) {
    sheetFrame(sheet, "ไม่ทราบผลการบันทึก", [el("p", { class: "note", role: "alert", text: "ยังไม่แน่ชัดว่าบันทึกสำเร็จหรือไม่ — ตรวจสถานะก่อนลองใหม่" })],
      [button("ตรวจสถานะ", async () => {
        const { status, data } = await api(`/api/review/cards/${encodeURIComponent(card.reqId)}`);
        if (status !== 200 || !data.ok) return renderUnknown(sheet, card, kind);
        const target = kind === "approve" ? "Approved" : "Changes requested";
        if (data.card.status === target) { if (kind === "request") clearDraft(card.reqId); return decisionDone(card, kind, { reconcile: {} }); }
        if (data.card.status === "Needs review") return kind === "approve" ? renderApprove(sheet, card, "ยังไม่ได้บันทึก — กดลองอีกครั้ง (ใช้คำขอเดิม ไม่บันทึกซ้ำ)") : renderRequest(sheet, card, "ยังไม่ได้บันทึก — กดส่งอีกครั้ง (ใช้คำขอเดิม)");
        return renderConflict(sheet, card, { code: "conflict_state", currentStatus: data.card.status });
      }, "primary", { "data-initial": true })]);
  }

  async function repairGate() {
    const project = app.reconcileError && app.reconcileError.project;
    if (!project) return;
    const { status, data } = await api("/api/review/reconcile", { method: "POST", body: { project } });
    if (status === 200 && data.ok) { app.reconcileError = null; toast(data.reconcile.message || "ซ่อมแล้ว"); }
    else toast(data.error || "ยังซ่อมไม่สำเร็จ");
    render();
  }

  // ---------- storage (sessionStorage only, design R4) ----------
  function sessionGet(key) { try { return sessionStorage.getItem(key); } catch (_) { return null; } }
  function sessionSet(key, value) { try { sessionStorage.setItem(key, value); } catch (_) {} }
  function readDraft(reqId) { try { return JSON.parse(sessionGet(draftKey(reqId)) || "null"); } catch (_) { return null; } }
  function writeDraft(reqId, text, hash) { if (text.trim()) sessionSet(draftKey(reqId), JSON.stringify({ text, hash })); else clearDraft(reqId); }
  function clearDraft(reqId) { try { sessionStorage.removeItem(draftKey(reqId)); } catch (_) {} }

  // ---------- auth states ----------
  function showLogin(expired) {
    app.queue = null; app.loggedIn = false;
    $("progress").classList.add("hidden"); $("actionBar").classList.add("hidden"); $("notices").replaceChildren();
    const user = el("input", { id: "loginUser", autocomplete: "username", "aria-label": "Username", placeholder: "Username" });
    const pass = el("input", { id: "loginPassword", type: "password", autocomplete: "current-password", "aria-label": "Password", placeholder: "Password" });
    const error = el("div", { class: "inline-error hidden", role: "alert", "aria-live": "assertive" });
    const submit = el("button", { class: "btn primary", type: "submit" }, "เข้าสู่ระบบ");
    const form = el("form", { class: "glass auth-card", onsubmit: async (event) => {
      event.preventDefault(); submit.disabled = true; error.classList.add("hidden");
      const res = await fetch("/api/login", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username: user.value, password: pass.value }) }).catch(() => null);
      let data = {}; try { data = res ? await res.json() : {}; } catch (_) {}
      submit.disabled = false;
      if (!res || !res.ok) { error.textContent = res && res.status === 429 ? "ลองผิดหลายครั้ง รอสักครู่แล้วลองใหม่" : (data.error || "เข้าสู่ระบบไม่สำเร็จ"); error.classList.remove("hidden"); return; }
      app.csrf = data.csrfToken; app.loggedIn = true; app.firstLoad = true;
      $("stage").replaceChildren(el("div", { class: "glass skeleton", "aria-label": "กำลังโหลดคิวรีวิว" }, [el("span"), el("span"), el("span")]));
      load();
    } }, [el("h2", { text: expired ? "เซสชันหมดอายุ" : "เข้าสู่ระบบ" }), el("p", { text: expired ? "เข้าสู่ระบบอีกครั้งเพื่อกลับมาที่คิวรีวิว ร่างเหตุผลยังเก็บไว้" : "ใช้บัญชี dashboard เดิม หน้านี้สำหรับ reviewer เท่านั้น" }), user, pass, error, submit]);
    $("stage").replaceChildren(form);
    user.focus();
  }

  function showForbidden() {
    app.queue = null;
    $("progress").classList.add("hidden"); $("actionBar").classList.add("hidden"); $("debugPanel").classList.add("hidden");
    $("stage").replaceChildren(notice("err", "บัญชีนี้ไม่มีสิทธิ์รีวิว requirement", "ติดต่อเจ้าของระบบถ้าคิดว่าควรมีสิทธิ์", [backButton()]));
  }

  // ---------- debug (Admin, mock only) ----------
  async function loadDebug() {
    const { status, data } = await api("/api/review/debug");
    if (status !== 200 || !data.ok) return;
    const select = $("debugSelect");
    select.replaceChildren(el("option", { value: "" }, "ปกติ (ไม่จำลอง error)"), ...Object.entries(data.options).map(([value, label]) => el("option", { value }, label)));
    select.value = data.simulate || "";
    $("debugPanel").classList.remove("hidden");
  }
  async function debugCall(body, message) {
    const { status, data } = await api("/api/review/debug", { method: "POST", body });
    toast(status === 200 && data.ok ? message : (data.error || "ทำไม่สำเร็จ"));
    if (body.action === "reset") { app.queue = null; app.updated.clear(); }
    // Keep the stale card on screen after "mutate" so the next decision hits the 409 path.
    if (body.action !== "mutate") load();
  }

  // ---------- viewport / keyboard ----------
  function syncViewport() {
    const vv = window.visualViewport;
    if (!vv) return;
    const inset = Math.max(0, window.innerHeight - vv.height - vv.offsetTop);
    document.documentElement.style.setProperty("--vvh", `${vv.height}px`);
    document.documentElement.style.setProperty("--kb-inset", `${inset}px`);
    const active = document.activeElement;
    if (active && active.id === "reasonInput") active.scrollIntoView({ block: "center" });
  }

  function shortcuts(event) {
    if (!$("sheet").classList.contains("hidden")) return trapFocus(event);
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    const tag = (event.target && event.target.tagName) || "";
    if (["INPUT", "TEXTAREA", "SELECT"].includes(tag)) return;
    if (!matchMedia("(min-width: 620px)").matches) return;
    const key = event.key.toLowerCase();
    if (key === "a" && !$("approveBtn").disabled) { event.preventDefault(); openApprove(); }
    else if (key === "r" && !$("requestBtn").disabled) { event.preventDefault(); openRequest(); }
    else if (key === "s" && !$("skipBtn").disabled) { event.preventDefault(); skipCurrent(); }
  }

  function init() {
    $("refreshBtn").addEventListener("click", () => load());
    $("approveBtn").addEventListener("click", () => openApprove());
    $("requestBtn").addEventListener("click", () => openRequest());
    $("skipBtn").addEventListener("click", skipCurrent);
    $("scrim").addEventListener("click", () => closeSheet());
    $("debugApply").addEventListener("click", () => { const value = $("debugSelect").value || null; debugCall({ action: "simulate", simulate: value }, value ? "เปิดการจำลองแล้ว" : "ปิดการจำลองแล้ว"); });
    $("debugMutate").addEventListener("click", () => { const card = current(); if (card) debugCall({ action: "mutate", reqId: card.reqId }, `จำลองการแก้ ${card.reqId} แล้ว — กด Approve เพื่อดู 409`); });
    $("debugReset").addEventListener("click", () => debugCall({ action: "reset" }, "ล้าง sandbox แล้ว"));
    document.addEventListener("keydown", shortcuts);
    if (window.visualViewport) { window.visualViewport.addEventListener("resize", syncViewport); window.visualViewport.addEventListener("scroll", syncViewport); syncViewport(); }
    ensureSession().then((ok) => (ok ? load() : showLogin(false)));
  }

  window.VedaReview = { app, load };
  init();
})();
