"use strict";
/* Sandbox 管理面板 — 零依赖 vanilla JS */

const TOKEN_KEY = "sbx_admin_sess";
const EXP_KEY = "sbx_admin_exp";
const POLL_MS = 15000;

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const ICON = {
  pause: '<svg viewBox="0 0 24 24" class="ico"><rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/></svg>',
  play: '<svg viewBox="0 0 24 24" class="ico"><polygon points="6 3 20 12 6 21 6 3"/></svg>',
  trash: '<svg viewBox="0 0 24 24" class="ico"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>',
  copy: '<svg viewBox="0 0 24 24" class="ico"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>',
  check: '<svg viewBox="0 0 24 24" class="ico"><polyline points="20 6 9 17 4 12"/></svg>',
  warn: '<svg viewBox="0 0 24 24" class="ico warn-ico"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
  info: '<svg viewBox="0 0 24 24" class="ico"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>',
  box: '<svg viewBox="0 0 24 24" class="ico"><path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 1 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/></svg>',
};

const token = () => sessionStorage.getItem(TOKEN_KEY) || "";

/* ---------------- API ---------------- */
async function api(path, { method = "GET", body, auth = true } = {}) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (auth && token()) headers["X-Admin-Token"] = token();
  let r;
  try {
    r = await fetch(path, { method, headers, body: body !== undefined ? JSON.stringify(body) : undefined });
  } catch {
    throw new Error("无法连接到控制面服务");
  }
  if (r.status === 401 && auth) { forceLogout("会话已过期，请重新登录"); throw new Error("会话已过期"); }
  if (!r.ok) {
    let msg = `HTTP ${r.status}`;
    try { const j = await r.json(); if (j.message) msg = j.message; } catch {}
    throw new Error(msg);
  }
  if (r.status === 204) return null;
  const ct = r.headers.get("content-type") || "";
  return ct.includes("json") ? r.json() : null;
}

/* ---------------- 视图切换 ---------------- */
function showLogin() {
  $("#view-login").hidden = false;
  $("#view-dash").hidden = true;
  stopPolling();
}
function showDash() {
  $("#view-login").hidden = true;
  $("#view-dash").hidden = false;
  loadAll();
  startPolling();
}
function forceLogout(msg) {
  sessionStorage.removeItem(TOKEN_KEY);
  sessionStorage.removeItem(EXP_KEY);
  showLogin();
  if (msg) toast(msg, "error");
}
function saveSession(tok, exp) {
  sessionStorage.setItem(TOKEN_KEY, tok);
  sessionStorage.setItem(EXP_KEY, String(exp));
}

/* ---------------- toast / modal ---------------- */
function toast(msg, type = "success") {
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.innerHTML = `${type === "error" ? ICON.warn : ICON.check}<span>${esc(msg)}</span>`;
  $("#toast-root").appendChild(el);
  setTimeout(() => { el.style.opacity = "0"; el.style.transition = "opacity .3s"; }, 3200);
  setTimeout(() => el.remove(), 3600);
}

function modal(html) {
  const root = $("#modal-root");
  root.innerHTML = `<div class="modal glass">${html}</div>`;
  root.hidden = false;
  const close = () => { root.hidden = true; root.innerHTML = ""; };
  root.onclick = (e) => { if (e.target === root) close(); };
  return close;
}

function confirmDialog(title, body, okText = "确认", danger = true) {
  return new Promise((resolve) => {
    const close = modal(`
      <h3>${ICON.warn}${esc(title)}</h3>
      <p>${esc(body)}</p>
      <div class="modal-actions">
        <button class="btn btn-ghost" data-x="cancel">取消</button>
        <button class="btn ${danger ? "btn-danger" : "btn-primary"}" data-x="ok">${esc(okText)}</button>
      </div>`);
    const root = $("#modal-root");
    root.querySelector('[data-x="cancel"]').onclick = () => { close(); resolve(false); };
    root.querySelector('[data-x="ok"]').onclick = () => { close(); resolve(true); };
  });
}

function showKeyOnce(plaintext) {
  modal(`
    <h3>${ICON.check}新 Key 已生成</h3>
    <div class="key-reveal">
      <code id="once-key">${esc(plaintext)}</code>
      <button class="btn btn-ghost icon-btn" id="copy-key" title="复制">${ICON.copy}</button>
    </div>
    <p class="key-warning">${ICON.warn}<span>完整 key 仅此一次显示，服务端只存哈希。请立即复制保存，事后无法找回。</span></p>
    <div class="modal-actions"><button class="btn btn-primary" data-x="done">我已保存</button></div>`);
  $("#copy-key").onclick = async () => {
    try { await navigator.clipboard.writeText(plaintext); toast("已复制到剪贴板"); }
    catch { const r = document.createRange(); r.selectNodeContents($("#once-key"));
      const s = getSelection(); s.removeAllRanges(); s.addRange(r); toast("请手动复制", "error"); }
  };
  $("#modal-root").querySelector('[data-x="done"]').onclick = () => {
    $("#modal-root").hidden = true; $("#modal-root").innerHTML = "";
  };
}

/* ---------------- 格式化 ---------------- */
function relTime(epochSec) {
  if (!epochSec) return "从未";
  const d = Math.max(0, Math.floor(Date.now() / 1000 - epochSec));
  if (d < 60) return "刚刚";
  if (d < 3600) return `${Math.floor(d / 60)} 分钟前`;
  if (d < 86400) return `${Math.floor(d / 3600)} 小时前`;
  return `${Math.floor(d / 86400)} 天前`;
}
function countdown(isoStr) {
  const ms = Date.parse(isoStr) - Date.now();
  if (ms <= 0) return "已超时";
  const s = Math.floor(ms / 1000);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(h)}:${pad(m)}:${pad(sec)}`;
}
const shortId = (id) => (id && id.length > 14 ? `${id.slice(0, 10)}…${id.slice(-4)}` : id);

/* ---------------- 渲染:健康 + 容量 ---------------- */
async function renderHealth() {
  let h;
  try { h = await api("/health", { auth: false }); }
  catch { $("#svc-list").innerHTML = `<li><span class="dot bad"></span>控制面<b>不可达</b></li>`; return; }
  const cap = h.capacity || {};
  const used = cap.used ?? 0, max = cap.max ?? "?";
  const pct = max ? Math.min(100, Math.round((used / max) * 100)) : 0;
  $("#cap-body").innerHTML = `
    <div class="cap-counts">
      <span class="cap-num">${used}</span><span class="cap-of">/ ${max} 个实例</span>
      <span class="cap-label" style="margin-left:auto">${pct}%</span>
    </div>
    <div class="gauge ${pct >= 80 ? "warn" : ""}"><i style="width:${pct}%"></i></div>
    <div class="gauge-meta"><span>实例容量</span></div>
    <div class="mini-stats">
      <div class="mini-stat"><b>${cap.cpu ?? 0}</b><span>已投入 CPU</span></div>
      <div class="mini-stat"><b>${cap.memoryMB ?? 0} MB</b><span>已投入内存</span></div>
    </div>`;
  const okRow = (label, ok, text) =>
    `<li><span class="dot ${ok ? "ok" : "bad"}"></span>${esc(label)}<b>${esc(text)}</b></li>`;
  $("#svc-list").innerHTML =
    okRow("控制面", h.ok, h.ok ? "在线" : "异常") +
    okRow("CRIU 快照", h.criu, h.criu ? "可用" : "不可用") +
    okRow("网络策略", h.netpolicy, h.netpolicy ? "可用" : "不可用");
}

/* ---------------- 渲染:API Keys ---------------- */
async function renderKeys() {
  const box = $("#keys-list");
  try {
    const data = await api("/admin/keys?includeRevoked=true");
    const keys = data.keys || [];
    const active = keys.filter((k) => !k.revokedAt);
    const tenants = new Set(active.map((k) => k.tenant));
    $("#kstat-body").innerHTML = `
      <div class="mini-stat"><b>${active.length}</b><span>有效 Key</span></div>
      <div class="mini-stat"><b>${tenants.size}</b><span>覆盖租户</span></div>
      <div class="mini-stat"><b>${keys.length - active.length}</b><span>已撤销</span></div>`;
    if (!keys.length) {
      box.innerHTML = `<div class="empty">${ICON.box}尚无凭证 — 用上方表单为租户签发第一个 Key</div>`;
      return;
    }
    box.innerHTML = keys.map((k) => `
      <div class="row" ${k.revokedAt ? 'style="opacity:.55"' : ""}>
        <div class="row-main">
          <div class="row-title">${esc(k.displayKey)}
            ${k.revokedAt ? '<span class="tag revoked">已撤销</span>' : ""}
          </div>
          <div class="row-sub">
            <span class="tag muted">${esc(k.owner)} / ${esc(k.tenant)}</span>
            ${k.label ? `<span>${esc(k.label)}</span>` : ""}
            <span>创建 ${relTime(k.createdAt)}</span>
            <span>最近使用 ${relTime(k.lastUsedAt)}</span>
          </div>
        </div>
        <div class="row-actions">
          ${k.revokedAt ? "" : `<button class="btn btn-ghost btn-sm" data-revoke="${esc(k.id)}">撤销</button>`}
        </div>
      </div>`).join("");
    box.querySelectorAll("[data-revoke]").forEach((btn) => {
      btn.onclick = async () => {
        const id = btn.getAttribute("data-revoke");
        if (!await confirmDialog("撤销此 API Key？",
            `撤销后 ${id} 对应的明文 key 立即永久失效，使用该 key 的服务将全部无法鉴权，且无法恢复。`, "撤销")) return;
        try { await api(`/admin/keys/${encodeURIComponent(id)}`, { method: "DELETE" });
          toast("Key 已撤销"); renderKeys(); }
        catch (e) { toast(e.message, "error"); }
      };
    });
  } catch (e) {
    if (e.message !== "会话已过期") box.innerHTML = `<div class="empty">${ICON.warn}${esc(e.message)}</div>`;
  }
}

/* ---------------- 渲染:沙箱 ---------------- */
async function renderSandboxes() {
  const box = $("#sbx-list");
  try {
    const data = await api("/admin/sandboxes");
    const rows = data.sandboxes || [];
    if (!rows.length) {
      box.innerHTML = `<div class="empty">${ICON.box}当前没有运行中或暂停的沙箱</div>`;
      return;
    }
    box.innerHTML = rows.map((s) => {
      const running = s.state === "running";
      return `
      <div class="row">
        <div class="row-main">
          <div class="row-title" title="${esc(s.sandboxID)}">${esc(shortId(s.sandboxID))}
            <span class="tag state ${running ? "" : "paused"}">
              ${running ? '<span class="dot ok" style="margin-right:5px"></span>' : ""}${esc(s.state)}</span>
          </div>
          <div class="row-sub">
            <span class="tag muted">${esc(s.owner)} / ${esc(s.tenant)}</span>
            <span>${esc(s.alias || s.templateID)}</span>
            <span>${s.cpuCount}C / ${s.memoryMB}MB</span>
            <span class="ttl" data-ttl="${esc(s.endAt)}">TTL ${countdown(s.endAt)}</span>
          </div>
        </div>
        <div class="row-actions">
          ${running
            ? `<button class="btn btn-ghost btn-sm icon-btn" title="暂停" data-pause="${esc(s.sandboxID)}">${ICON.pause}</button>`
            : `<button class="btn btn-ghost btn-sm icon-btn" title="恢复" data-resume="${esc(s.sandboxID)}">${ICON.play}</button>`}
          <button class="btn btn-danger btn-sm icon-btn" title="销毁" data-kill="${esc(s.sandboxID)}">${ICON.trash}</button>
        </div>
      </div>`;
    }).join("");
    box.querySelectorAll("[data-pause]").forEach((b) => b.onclick = () => sbxAction(b, "pause", "已暂停"));
    box.querySelectorAll("[data-resume]").forEach((b) => b.onclick = () => sbxAction(b, "resume", "已恢复"));
    box.querySelectorAll("[data-kill]").forEach((b) => b.onclick = async () => {
      const id = b.getAttribute("data-kill");
      if (!await confirmDialog("销毁沙箱？",
          `${id} 的容器与状态将被永久删除，数据不可恢复。`, "销毁")) return;
      sbxAction(b, null, "已销毁", "DELETE", `/admin/sandboxes/${encodeURIComponent(id)}`);
    });
  } catch (e) {
    if (e.message !== "会话已过期") box.innerHTML = `<div class="empty">${ICON.warn}${esc(e.message)}</div>`;
  }
}

async function sbxAction(btn, verb, okMsg, method, path) {
  btn.disabled = true;
  try {
    if (path) await api(path, { method: method || "POST" });
    else await api(`/admin/sandboxes/${encodeURIComponent(btn.getAttribute(`data-${verb}`))}/${verb}`,
                   { method: "POST" });
    toast(okMsg);
  } catch (e) { toast(e.message, "error"); btn.disabled = false; }
  setTimeout(loadSandboxOnly, 800);
}

/* ---------------- 渲染:Key 申请审批 ---------------- */
const KREQ_TAG = {
  pending: '<span class="tag pending">待审批</span>',
  approved: '<span class="tag ok-state">已批准</span>',
  rejected: '<span class="tag revoked">已驳回</span>',
  cancelled: '<span class="tag muted">已撤回</span>',
};

function rejectDialog(rid) {
  const close = modal(`
    <h3>${ICON.warn}驳回申请</h3>
    <p>驳回理由将展示给申请人（凭 ticket 查询时可见），请写明原因。</p>
    <div class="field"><textarea id="reject-reason" rows="3"
      placeholder="如: owner/tenant 无法核实"></textarea></div>
    <div class="modal-actions">
      <button class="btn btn-ghost" data-x="cancel">取消</button>
      <button class="btn btn-danger" data-x="ok">驳回</button>
    </div>`);
  const root = $("#modal-root");
  root.querySelector('[data-x="cancel"]').onclick = close;
  root.querySelector('[data-x="ok"]').onclick = async () => {
    const reason = root.querySelector("#reject-reason").value.trim();
    if (!reason) { toast("请填写驳回理由", "error"); return; }
    close();
    try { await api(`/admin/key-requests/${encodeURIComponent(rid)}/reject`,
                     { method: "POST", body: { reason } });
      toast("已驳回"); renderKeyRequests(); }
    catch (e) { toast(e.message, "error"); }
  };
}

async function renderKeyRequests() {
  const box = $("#kreq-list");
  try {
    const data = await api("/admin/key-requests");
    const reqs = data.requests || [];
    const pending = reqs.filter((r) => r.status === "pending");
    const badge = $("#kreq-pending");
    badge.hidden = !pending.length;
    badge.textContent = `${pending.length} 待审批`;
    if (!reqs.length) {
      box.innerHTML = `<div class="empty">${ICON.box}暂无申请 — 第三方可经 POST /keys/requests 提交</div>`;
      return;
    }
    box.innerHTML = reqs.map((r) => `
      <div class="row" ${r.status !== "pending" ? 'style="opacity:.6"' : ""}>
        <div class="row-main">
          <div class="row-title">${esc(r.requestID)} ${KREQ_TAG[r.status] || esc(r.status)}</div>
          <div class="row-sub">
            <span class="tag muted">${esc(r.owner)} / ${esc(r.tenant)}</span>
            <span>申请人 ${esc(r.applicant)}</span>
            ${r.label ? `<span>${esc(r.label)}</span>` : ""}
            ${r.note ? `<span>备注: ${esc(r.note)}</span>` : ""}
            ${r.status === "rejected" && r.rejectReason ? `<span>理由: ${esc(r.rejectReason)}</span>` : ""}
            ${r.issuedKeyID ? `<span>签发 ${esc(r.issuedKeyID)}</span>` : ""}
            <span>提交于 ${relTime(r.createdAt)}</span>
          </div>
        </div>
        <div class="row-actions">
          ${r.status === "pending" ? `
            <button class="btn btn-primary btn-sm" data-approve="${esc(r.requestID)}">批准</button>
            <button class="btn btn-danger btn-sm" data-reject="${esc(r.requestID)}">驳回</button>` : ""}
        </div>
      </div>`).join("");
    box.querySelectorAll("[data-approve]").forEach((btn) => {
      btn.onclick = async () => {
        const rid = btn.getAttribute("data-approve");
        const r = reqs.find((x) => x.requestID === rid);
        if (!await confirmDialog("批准此申请并签发 Key？",
            `将为 ${r.owner} / ${r.tenant}（申请人 ${r.applicant}）签发新 Key。` +
            "明文只暂存待领取，由申请人凭 ticket 一次性取走，面板不经手明文。",
            "批准", false)) return;
        btn.disabled = true;
        try {
          const data = await api(`/admin/key-requests/${encodeURIComponent(rid)}/approve`,
                                 { method: "POST", body: {} });
          toast(`已批准，签发 ${data.issuedKeyID}`); renderKeyRequests(); renderKeys();
        } catch (e) { toast(e.message, "error"); btn.disabled = false; }
      };
    });
    box.querySelectorAll("[data-reject]").forEach((btn) => {
      btn.onclick = () => rejectDialog(btn.getAttribute("data-reject"));
    });
  } catch (e) {
    if (e.message !== "会话已过期") box.innerHTML = `<div class="empty">${ICON.warn}${esc(e.message)}</div>`;
  }
}

/* ---------------- 加载编排 ---------------- */
async function loadAll() {
  $("#cap-body").innerHTML = '<div class="skeleton sk-row"></div>';
  await Promise.all([renderHealth(), renderKeys(), renderSandboxes(), renderKeyRequests()]);
}
async function loadSandboxOnly() {
  await Promise.all([renderHealth(), renderSandboxes(), renderKeyRequests()]);
}

let pollTimer = null;
function startPolling() { stopPolling(); pollTimer = setInterval(loadSandboxOnly, POLL_MS); }
function stopPolling() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }

/* TTL 秒级滴答（纯文本更新，不重排） */
setInterval(() => {
  document.querySelectorAll("[data-ttl]").forEach((el) => {
    el.textContent = `TTL ${countdown(el.getAttribute("data-ttl"))}`;
  });
}, 1000);

/* 会话倒计时 */
setInterval(() => {
  const exp = Number(sessionStorage.getItem(EXP_KEY) || 0);
  if (!exp) return;
  const left = Math.floor(exp - Date.now() / 1000);
  if (left <= 0) { forceLogout("会话已过期，请重新登录"); return; }
  const h = Math.floor(left / 3600), m = Math.floor((left % 3600) / 60), s = left % 60;
  $("#session-timer").textContent = `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}, 1000);

/* ---------------- 事件绑定 ---------------- */
$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("#login-btn"), errEl = $("#login-error");
  errEl.hidden = true; btn.disabled = true;
  try {
    const data = await api("/admin/login", {
      method: "POST", auth: false,
      body: { username: $("#login-user").value.trim(), password: $("#login-pass").value },
    });
    saveSession(data.token, data.expiresAt);
    $("#login-pass").value = "";
    showDash();
  } catch (err) {
    errEl.textContent = err.message; errEl.hidden = false;
  } finally { btn.disabled = false; }
});

$("#logout-btn").onclick = () => forceLogout();
$("#keys-refresh").onclick = renderKeys;
$("#sbx-refresh").onclick = renderSandboxes;
$("#kreq-refresh").onclick = renderKeyRequests;

$("#key-create-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const owner = $("#kc-owner").value.trim(), tenant = $("#kc-tenant").value.trim();
  if (!owner || !tenant) { toast("owner 与 tenant 必填", "error"); return; }
  try {
    const data = await api("/admin/keys", {
      method: "POST",
      body: { owner, tenant, label: $("#kc-label").value.trim() },
    });
    $("#kc-owner").value = $("#kc-tenant").value = $("#kc-label").value = "";
    showKeyOnce(data.key);
    renderKeys();
  } catch (err) { toast(err.message, "error"); }
});

/* ---------------- 启动 ---------------- */
if (token() && Number(sessionStorage.getItem(EXP_KEY)) * 1000 > Date.now()) showDash();
else showLogin();
