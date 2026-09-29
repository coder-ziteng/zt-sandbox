"""Sandbox service control plane — E2B compatible API (reference: Aliyun Bailian Sandbox).

P1: browser / all-in-one templates (container port 3000) + /health polling.
P2: network allowlist per template, CRIU-aware pause/resume, admission control (quota).
"""
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timezone

import netpolicy
import diagnostics
import metrics
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import store
import runtime

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("control-plane")

API_KEYS = [k.strip() for k in os.getenv("API_KEYS", "").split(",") if k.strip()]
# --- P4: separate admin credential for /admin/* — never accepted on data-plane
# endpoints. Operators configure it via the ADMIN_TOKEN env var (long random
# string); rotate by restarting the control plane with a new value.
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()
# P4 第七刀: 管理面板登录凭据 — 由 deploy_server.py 从 SBX_SSH_USER /
# SBX_SSH_PASSWORD 写入 deploy/.env。登录成功后签发以 ADMIN_TOKEN 为密钥的
# HMAC 短效 session token，浏览器永远接触不到 ADMIN_TOKEN 本身。
ADMIN_USER = os.getenv("ADMIN_USER", "").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()
ADMIN_SESSION_TTL_S = int(os.getenv("ADMIN_SESSION_TTL_S", "28800"))  # 8h
SANDBOX_DOMAIN = os.getenv("SANDBOX_DOMAIN", "192.168.2.162.nip.io")
ENVD_VERSION = "0.7.0"
MAX_SANDBOXES = int(os.getenv("MAX_SANDBOXES", "24"))
MAX_MEMORY_MB = int(os.getenv("MAX_MEMORY_MB", "6144"))
HOOK_TICK_S = int(os.getenv("HOOK_TICK_S", "30"))
NETPOLICY_REFRESH_S = int(os.getenv("NETPOLICY_REFRESH_S", "300"))  # 5min

# Optional identity binding: API_KEYS_JSON='key1:owner1:tenant1;key2:owner2:tenant2'
# Format: semicolon-separated entries of `<key>:<owner>:<tenant>`. The colon
# format avoids the quote-stripping pitfalls of writing JSON through docker
# compose's env-file parser. When unset, every key gets (default, default) —
# single-key legacy mode where sandbox ownership is not enforced.
#
# P4: admin-managed keys (api_keys table) take precedence at request time. At
# startup we *preload* env keys into the DB so the env is the bootstrap source
# of truth — once the operator uses /admin/keys to mint or revoke, the DB
# becomes authoritative. Legacy env keys that already exist in the DB are
# detected by (prefix,suffix) and re-attached to the row instead of duplicated.
OWNER_MAP: dict[str, tuple[str, str]] = {}
_key_json = os.getenv("API_KEYS_JSON", "").strip()
_env_keys: list[tuple[str, str, str, str]] = []  # (plaintext, owner, tenant, label)
if _key_json:
    try:
        for entry in _key_json.split(";"):
            entry = entry.strip()
            if not entry:
                continue
            parts = entry.split(":")
            if len(parts) < 3:
                raise ValueError(f"malformed entry: {entry!r}")
            k, owner, tenant = parts[0], parts[1], parts[2]
            OWNER_MAP[k] = (owner, tenant)
            if k not in API_KEYS:
                API_KEYS.append(k)
            _env_keys.append((k, owner, tenant, "from-env"))
        # Keys in API_KEYS but absent from JSON still get (default, default)
        # for backward compatibility with legacy single-key deployments.
        for k in API_KEYS:
            OWNER_MAP.setdefault(k, ("default", "default"))
    except Exception as e:
        log.exception("API_KEYS_JSON 解析失败,回退到 API_KEYS: %s", e)

ISOLATION_ENABLED = any(o != "default" or t != "default" for o, t in OWNER_MAP.values())

app = FastAPI(title="sandbox-service", docs_url=None, redoc_url=None)
store.init_db()
runtime.ensure_network()


def _seed_env_keys_into_db():
    """Bootstrap: copy legacy env-var keys into the api_keys table so the admin
    interface can list/revoke them. Idempotent — re-running with the same env
    leaves existing rows alone (matched by hash)."""
    if not _env_keys:
        return
    for plain, owner, tenant, label in _env_keys:
        h = store._hash_key(plain)
        with store.db() as c:
            existing = c.execute(
                "SELECT id, revoked_at FROM api_keys WHERE key_hash=?", (h,)
            ).fetchone()
            if existing:
                continue
            kid = "k_" + plain[:8].replace("e2b_", "").replace("_", "")[:12] or "kenv"
            c.execute(
                "INSERT INTO api_keys (id,key_hash,prefix,suffix,owner,tenant,label,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (kid, h, plain[:8], plain[-4:], owner, tenant, label, time.time()),
            )


_seed_env_keys_into_db()


def err(code: int, message: str, status: int):
    rid = f"req-{int(time.time()*1000)}"
    return JSONResponse({"code": code, "message": message, "requestID": rid}, status_code=status)


@app.middleware("http")
async def auth(request: Request, call_next):
    path = request.url.path
    if path.startswith("/admin"):
        # /admin/* uses a separate credential — never the data-plane API key.
        # P4 第七刀: 登录入口与管理面板静态资源豁免; 其余仍要求
        # ADMIN_TOKEN 或有效签名的 session token (数据面 key 依旧被拒)。
        if path == "/admin/login" or path.startswith("/admin/ui"):
            return await call_next(request)
        if not ADMIN_TOKEN:
            return err(100012, "admin 未启用: 未设置 ADMIN_TOKEN", 503)
        presented = request.headers.get("x-admin-token", "") or _bearer(request)
        if not presented or (presented != ADMIN_TOKEN and not _verify_session(presented)):
            return err(100012, "admin 凭证无效", 401)
        return await call_next(request)
    if path in ("/health", "/metrics", "/") or path.startswith("/internal"):
        return await call_next(request)
    # P4 第八刀: 自助申请 API Key 的公开端点。申请时调用方还没有任何凭证,
    # 故豁免数据面鉴权; 后续查状态/领取/撤回凭一次性 ticket header 自证。
    if path == "/keys/requests" or path.startswith("/keys/requests/"):
        return await call_next(request)
    # Two credential styles are accepted:
    #   Authorization: Bearer <key>   (Bailian / our REST convention)
    #   X-API-KEY: <key>              (what the official e2b SDK actually sends)
    authz = request.headers.get("authorization", "")
    token = authz[7:] if authz.lower().startswith("bearer ") else ""
    if not token:
        token = request.headers.get("x-api-key", "")
    if not token:
        return err(100001, "API Key 无效", 401)
    # P4: try DB-managed key (hashed) first — these override env keys at
    # request time so admin revoke takes effect immediately.
    identity = store.resolve_key(token)
    if identity is not None:
        request.state.owner, request.state.tenant = identity
        return await call_next(request)
    # Fall back to legacy env-var list (key is plaintext in the list).
    if token in API_KEYS:
        request.state.owner, request.state.tenant = OWNER_MAP.get(token, ("default", "default"))
        return await call_next(request)
    return err(100001, "API Key 无效", 401)


def _bearer(request: Request) -> str:
    authz = request.headers.get("authorization", "")
    return authz[7:] if authz.lower().startswith("bearer ") else ""


def caller_identity(request: Request) -> tuple[str, str]:
    return getattr(request.state, "owner", "default"), getattr(request.state, "tenant", "default")


def check_owner(request: Request, row: dict):
    """Enforce (a) chat-session binding and (b) owner/tenant isolation on a
    sandbox row. Returns None on pass, or 403 JSONResponse.

    Session check (P4 第六刀) applies to ALL modes whenever the row is bound
    to a session_id — the caller must present a matching X-Session-Id header.
    Unbound (legacy) sandboxes pass through.

    Owner/tenant check (P4 第四刀) is skipped in legacy single-key mode
    (ISOLATION_ENABLED=False) — everyone maps to (default, default).
    """
    row_session = (row.get("session_id") if row else None) or None
    if row_session:
        req_session = request.headers.get("x-session-id", "").strip()
        if not req_session:
            return err(100013, "沙箱绑定了 session, 请提供 X-Session-Id header", 403)
        if req_session != row_session:
            return err(100013,
                       f"X-Session-Id 不匹配 (caller={req_session}, sandbox={row_session})",
                       403)
    if not ISOLATION_ENABLED:
        return None
    row_owner = (row.get("owner") if row else None) or "default"
    row_tenant = (row.get("tenant") if row else None) or "default"
    req_owner, req_tenant = caller_identity(request)
    if row_owner != req_owner or row_tenant != req_tenant:
        return err(100011,
                   f"无权访问此沙箱 (caller={req_owner}/{req_tenant}, sandbox={row_owner}/{row_tenant})",
                   403)
    return None


@app.middleware("http")
async def record_http(request: Request, call_next):
    """HTTP 流量观测: 每次请求记一次 counter + histogram。
    /metrics 端点本身不记录 (避免自激)。"""
    path = request.url.path
    if path == "/metrics":
        return await call_next(request)
    t0 = time.time()
    response = await call_next(request)
    dt = time.time() - t0
    endpoint = metrics.normalize_endpoint(path)
    metrics.HTTP_REQUESTS.labels(method=request.method, endpoint=endpoint,
                                  status=response.status_code).inc()
    metrics.HTTP_DURATION.labels(method=request.method, endpoint=endpoint).observe(dt)
    return response


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sandbox_json(row: dict, with_token: bool = True) -> dict:
    tpl = store.get_template(row["template_code"])
    d = {
        "clientID": row["client_id"],
        "sandboxID": row["sandbox_id"],
        "templateID": row["template_code"],
        "alias": tpl["name"] if tpl else row["template_code"],
        "envdVersion": row["envd_version"],
        "domain": SANDBOX_DOMAIN,
        "metadata": json.loads(row["metadata"] or "{}"),
        "startedAt": iso(row["started_at"]),
        "endAt": iso(row["end_at"]),
        "state": row["state"],
        "cpuCount": tpl["cpu_count"] if tpl else 1,
        "memoryMB": tpl["memory_mb"] if tpl else 2048,
        "diskSizeMB": tpl["disk_size_mb"] if tpl else 2048,
        "features": row.get("features") or "envd,jupyter",
        "pauseMode": row.get("pause_mode") or "stop",
    }
    if row.get("session_id"):
        d["sessionID"] = row["session_id"]
    if row.get("host_port_browser"):
        d["browserPort"] = 3000
    if with_token:
        d["envdAccessToken"] = row["envd_token"]
    hs = json.loads(row.get("hook_state") or "{}")
    if hs:
        d["hookState"] = hs
    return d


# ---------------- lifecycle ----------------

@app.get("/health")
def health():
    return {
        "ok": True,
        "criu": runtime.criu_available(),
        "netpolicy": netpolicy.supported(),
        "capacity": {"max": MAX_SANDBOXES, "used": store.count_active(), **store.committed_resources()},
    }


@app.get("/metrics")
def prometheus_metrics():
    """Prometheus-format metrics — 沙箱生命周期 / HTTP 流量 / 网络策略 / 钩子 / 诊断。

    任何 Prometheus 兼容的 TSDB 都可以直接抓取这个端点
    (Prometheus / VictoriaMetrics / Datadog Agent / OTel Collector 的
    prometheus receiver)。无需鉴权 — 指标不含敏感信息。
    """
    return Response(content=metrics.render(), media_type=metrics.CONTENT_TYPE)


# ---------------- /admin/* (P4 — credential management) ----------------
# Separate credential (ADMIN_TOKEN) — API_KEYS are not accepted here on
# purpose, so a leaked data-plane key can never mint new ones. Operators
# should rotate ADMIN_TOKEN by restarting the control plane.

@app.get("/admin/keys")
def admin_list_keys(includeRevoked: bool = False):
    return {"keys": store.list_api_keys(include_revoked=includeRevoked)}


@app.post("/admin/keys")
async def admin_create_key(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    owner = (body.get("owner") or "").strip()
    tenant = (body.get("tenant") or "").strip()
    label = (body.get("label") or "").strip()
    if not owner or not tenant:
        return err(100013, "owner/tenant 必填", 400)
    meta, plaintext = store.generate_api_key(owner, tenant, label)
    # Plaintext is returned ONCE — caller (operator) must store it. The list
    # endpoint will never display it again; revocation means the plaintext is
    # permanently dead, no recovery.
    return JSONResponse(
        {
            "key": plaintext,
            "meta": meta,
            "warning": "完整 key 仅此一次返回。请立即保存到安全的地方,事后无法再读取。",
        },
        status_code=201,
    )


@app.delete("/admin/keys/{key_id}")
def admin_revoke_key(key_id: str):
    if not store.revoke_api_key(key_id):
        return err(100014, f"key 不存在或已撤销: {key_id}", 404)
    return {"id": key_id, "revoked": True}


# ---------------- key self-service requests (P4 第八刀) ----------------
# 第三方 agent/调用方没有管理凭证,不能直接 mint key,但可以走公开申请流:
#   POST   /keys/requests             落库 pending,一次性返回 ticket
#   GET    /keys/requests/{rid}       凭 X-Request-Ticket 查进度
#   POST   /keys/requests/{rid}/claim 审批通过后凭 ticket 一次性领取明文 key
#   DELETE /keys/requests/{rid}       申请人撤回 pending 申请
# mint 只发生在管理员 approve 一刻; approve 响应不含明文,明文暂存 outbox
# 列,claim 后清空 (项目本就以明文存 envd_token,风险面一致)。

_req_throttle: dict[str, list[float]] = {}
_req_throttle_lock = threading.Lock()
KEY_REQUEST_RATE_LIMIT = int(os.getenv("KEY_REQUEST_RATE_LIMIT", "10"))
KEY_REQUEST_RATE_WINDOW_S = int(os.getenv("KEY_REQUEST_RATE_WINDOW_S", "300"))
KEY_REQUEST_MAX_PENDING = int(os.getenv("KEY_REQUEST_MAX_PENDING", "5"))


def _req_throttled(ip: str) -> bool:
    now = time.time()
    with _req_throttle_lock:
        hits = [t for t in _req_throttle.get(ip, []) if now - t < KEY_REQUEST_RATE_WINDOW_S]
        hits.append(now)
        _req_throttle[ip] = hits
        return len(hits) > KEY_REQUEST_RATE_LIMIT


def _request_ticket_ok(request: Request, rid: str) -> bool:
    return store.verify_request_ticket(rid, request.headers.get("x-request-ticket", "").strip())


@app.post("/keys/requests")
async def submit_key_request(request: Request):
    ip = request.client.host if request.client else "?"
    if _req_throttled(ip):
        return err(100016, f"申请过于频繁 (每 {KEY_REQUEST_RATE_WINDOW_S}s 最多 {KEY_REQUEST_RATE_LIMIT} 次)", 429)
    try:
        body = await request.json()
    except Exception:
        body = {}
    applicant = str(body.get("applicant") or "").strip()
    owner = str(body.get("owner") or "").strip()
    tenant = str(body.get("tenant") or "").strip()
    label = str(body.get("label") or "").strip()
    note = str(body.get("note") or "").strip()
    if not applicant or not owner or not tenant:
        return err(100013, "applicant/owner/tenant 必填", 400)
    if (len(applicant) > 128 or len(owner) > 64 or len(tenant) > 64
            or len(label) > 64 or len(note) > 512):
        return err(100013, "字段超长 (applicant≤128, owner/tenant/label≤64, note≤512)", 400)
    if store.pending_count_for_owner(owner) >= KEY_REQUEST_MAX_PENDING:
        return err(100016, f"owner '{owner}' 已有 {KEY_REQUEST_MAX_PENDING} 个待审批申请,请等待处理", 409)
    ticket = secrets.token_urlsafe(24)
    rid = store.create_key_request(applicant, owner, tenant, label, note, ticket)
    return JSONResponse(
        {
            "requestID": rid,
            "ticket": ticket,
            "status": "pending",
            "warning": "ticket 仅此一次返回,用于查询进度与领取 key,请立即保存。",
        },
        status_code=201,
    )


@app.get("/keys/requests/{rid}")
def fetch_key_request(rid: str, request: Request):
    if not _request_ticket_ok(request, rid) or store.get_key_request(rid) is None:
        return err(100017, "ticket 无效或申请不存在", 403)
    return store.get_key_request(rid)


@app.post("/keys/requests/{rid}/claim")
def claim_issued_key(rid: str, request: Request):
    if not _request_ticket_ok(request, rid):
        return err(100017, "ticket 无效或申请不存在", 403)
    plaintext = store.claim_key_request(rid)
    if plaintext is None:
        row = store.get_key_request(rid)
        status = row["status"] if row else "unknown"
        if status == "approved":
            return err(100017, "key 已被领取过,明文不可恢复", 410)
        return err(100017, f"申请状态为 {status},尚无可领取的 key", 409)
    return JSONResponse(
        {
            "key": plaintext,
            "meta": store.get_key_request(rid),
            "warning": "完整 key 仅此一次返回,请立即保存到安全的地方。",
        }
    )


@app.delete("/keys/requests/{rid}")
def withdraw_key_request(rid: str, request: Request):
    if not _request_ticket_ok(request, rid):
        return err(100017, "ticket 无效或申请不存在", 403)
    if not store.cancel_key_request(rid):
        row = store.get_key_request(rid)
        status = row["status"] if row else "unknown"
        return err(100017, f"仅 pending 申请可撤回 (当前状态: {status})", 409)
    return {"requestID": rid, "status": "cancelled"}


@app.get("/admin/key-requests")
def admin_list_key_requests(status: str = ""):
    return {"requests": store.list_key_requests(status or None)}


@app.post("/admin/key-requests/{rid}/approve")
async def admin_approve_key_request(rid: str, request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    row = store.get_key_request(rid)
    if not row:
        return err(100014, f"申请不存在: {rid}", 404)
    owner = str(body.get("owner") or row["owner"]).strip()
    tenant = str(body.get("tenant") or row["tenant"]).strip()
    label = str(body.get("label") or row["label"]).strip()
    meta, plaintext = store.generate_api_key(owner, tenant, label)
    # 状态守卫在 store 层 (WHERE status='pending'); 并发/重复审批时
    # 回滚刚 mint 的 key,避免留下孤儿凭证。
    if not store.approve_key_request(rid, meta["id"], plaintext,
                                     owner=owner, tenant=tenant, label=label):
        store.revoke_api_key(meta["id"])
        fresh = store.get_key_request(rid)
        return err(100014, f"申请状态为 {fresh['status'] if fresh else 'unknown'},仅 pending 可审批", 409)
    return {"requestID": rid, "status": "approved", "issuedKeyID": meta["id"],
            "owner": owner, "tenant": tenant, "label": label,
            "note": "明文已暂存,申请人凭 ticket 一次性领取; 本响应不含明文。"}


@app.post("/admin/key-requests/{rid}/reject")
async def admin_reject_key_request(rid: str, request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    reason = str(body.get("reason") or "").strip()
    if not reason:
        return err(100013, "reason 必填", 400)
    if not store.reject_key_request(rid, reason[:512]):
        row = store.get_key_request(rid)
        if not row:
            return err(100014, f"申请不存在: {rid}", 404)
        return err(100014, f"申请状态为 {row['status']},仅 pending 可驳回", 409)
    return {"requestID": rid, "status": "rejected"}


# ---------------- /admin panel (P4 第七刀 — 管理面板) ----------------

def _session_sig(exp: int) -> str:
    return hmac.new(ADMIN_TOKEN.encode(), f"sbxsess.{exp}".encode(), hashlib.sha256).hexdigest()


def _issue_session() -> tuple[str, int]:
    exp = int(time.time()) + ADMIN_SESSION_TTL_S
    return f"sbxsess.{exp}.{_session_sig(exp)}", exp


def _verify_session(token: str) -> bool:
    """Validate `sbxsess.<exp>.<hmac>` — signed with ADMIN_TOKEN as key."""
    if not ADMIN_TOKEN or not token.startswith("sbxsess."):
        return False
    parts = token.split(".")
    if len(parts) != 3:
        return False
    try:
        exp = int(parts[1])
    except ValueError:
        return False
    if exp <= time.time():
        return False
    return hmac.compare_digest(_session_sig(exp), parts[2])


@app.post("/admin/login")
async def admin_login(request: Request):
    """Panel entry: verify ADMIN_USER/ADMIN_PASSWORD (= SBX_SSH_USER/PASSWORD
    wired by deploy_server.py) and return a short-lived signed session token.
    The browser never sees ADMIN_TOKEN itself."""
    if not ADMIN_TOKEN or not ADMIN_USER or not ADMIN_PASSWORD:
        return err(100012, "管理面板未启用: 缺少 ADMIN_TOKEN/ADMIN_USER/ADMIN_PASSWORD", 503)
    try:
        body = await request.json()
    except Exception:
        body = {}
    username = str(body.get("username") or "")
    password = str(body.get("password") or "")
    ok_user = hmac.compare_digest(username, ADMIN_USER)
    ok_pass = hmac.compare_digest(password, ADMIN_PASSWORD)
    if not (ok_user and ok_pass):
        log.warning("admin login failed for user %r from %s", username,
                    request.client.host if request.client else "?")
        return err(100012, "用户名或密码无效", 401)
    token, exp = _issue_session()
    log.info("admin session issued for %r (expires %s)", username, iso(exp))
    return {"token": token, "expiresAt": exp}


def _admin_sandbox_json(row: dict) -> dict:
    d = sandbox_json(row, with_token=False)
    d.pop("envdAccessToken", None)
    d["owner"] = row.get("owner") or "default"
    d["tenant"] = row.get("tenant") or "default"
    return d


@app.get("/admin/sandboxes")
def admin_list_sandboxes():
    """Full inventory across all owners/tenants (admin view, no envd tokens)."""
    rows = store.list_sandboxes(("running", "paused"))
    return {"sandboxes": [_admin_sandbox_json(r) for r in rows]}


@app.post("/admin/sandboxes/{sandbox_id}/pause")
async def admin_pause_sandbox(sandbox_id: str, request: Request):
    row = store.get_sandbox(sandbox_id)
    if not row:
        return err(100003, f"沙箱不存在: {sandbox_id}", 404)
    if row["state"] == "paused":
        return Response(status_code=409)
    try:
        body = await request.json()
    except Exception:
        body = {}
    use_criu = str(body.get("criu", "auto")).lower() in ("1", "true", "auto", "yes")
    mode = "stop"
    if use_criu and runtime.criu_available():
        if runtime.checkpoint_container(sandbox_id):
            mode = "criu"
        else:
            log.warning("CRIU checkpoint failed for %s, using docker stop", sandbox_id)
    if mode == "stop":
        runtime.stop_container(sandbox_id)
    store.update_sandbox(sandbox_id, state="paused", pause_mode=mode)
    metrics.SANDBOX_ACTIVE.labels(state="running").dec()
    metrics.SANDBOX_ACTIVE.labels(state="paused").inc()
    log.info("admin paused sandbox %s (mode=%s)", sandbox_id, mode)
    return Response(status_code=204)


@app.post("/admin/sandboxes/{sandbox_id}/resume")
async def admin_resume_sandbox(sandbox_id: str, request: Request):
    row = store.get_sandbox(sandbox_id)
    if not row:
        return err(100003, f"沙箱不存在: {sandbox_id}", 404)
    try:
        body = await request.json()
    except Exception:
        body = {}
    timeout = int(body.get("timeout") or 300)
    tpl = store.get_template(row["template_code"])
    ok, msg = _resume_data_plane(row, tpl)
    if not ok:
        return err(100006, msg, 404 if "不存在" in msg else 503)
    store.update_sandbox(sandbox_id, state="running", end_at=time.time() + timeout,
                         last_activity=time.time())
    row = store.get_sandbox(sandbox_id)
    metrics.SANDBOX_ACTIVE.labels(state="paused").dec()
    metrics.SANDBOX_ACTIVE.labels(state="running").inc()
    log.info("admin resumed sandbox %s", sandbox_id)
    return _admin_sandbox_json(row)


@app.delete("/admin/sandboxes/{sandbox_id}")
def admin_kill_sandbox(sandbox_id: str):
    row = store.get_sandbox(sandbox_id)
    if not row:
        return Response(status_code=404)
    prev_state = row.get("state", "running")
    runtime.remove_container(sandbox_id)
    store.delete_sandbox(sandbox_id)
    log.info("admin killed sandbox %s", sandbox_id)
    metrics.SANDBOX_DESTROYED.labels(reason="admin").inc()
    if prev_state in ("running", "paused"):
        metrics.SANDBOX_ACTIVE.labels(state=prev_state).dec()
    return Response(status_code=204)


_UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "admin-ui")
if os.path.isdir(_UI_DIR):
    app.mount("/admin/ui", StaticFiles(directory=_UI_DIR, html=True), name="admin-ui")


@app.post("/sandboxes")
async def create_sandbox(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    template_id = body.get("templateID") or body.get("template_id")
    if not template_id:
        return err(100004, "参数缺失: templateID", 400)
    tpl = store.get_template(template_id) or store.get_template_by_name(template_id)
    if not tpl:
        return err(100002, f"模版不存在: {template_id}", 404)

    # --- admission control (P2) ---
    used = store.committed_resources()
    if used["count"] >= MAX_SANDBOXES:
        return err(100009, f"沙箱数量已达上限 {MAX_SANDBOXES}", 429)
    if used["memoryMB"] + int(tpl["memory_mb"]) > MAX_MEMORY_MB:
        return err(100009, f"内存配额不足: 已用 {used['memoryMB']}MB / 上限 {MAX_MEMORY_MB}MB", 429)

    timeout = int(body.get("timeout") or 600)
    metadata = body.get("metadata") or {}
    env_vars = body.get("env_vars") or {}
    client_id = store.new_id("cli")

    # P4 第六刀: optional chat-session binding. Same (owner, tenant, sessionId)
    # cannot have two live sandboxes at once — the caller's chat platform owns
    # the session→sandbox mapping.
    session_id = (body.get("sessionId") or body.get("session_id") or "").strip() or None
    if session_id:
        owner_, tenant_ = caller_identity(request)
        existing = store.find_active_sandbox_by_session(owner_, tenant_, session_id)
        if existing:
            return err(100015,
                       f"该 session 已有沙箱 (sandboxID={existing['sandbox_id']}); "
                       f"复用或先销毁",
                       409)

    sandbox_id = store.new_id("sbx")
    envd_token = store.new_id("tok")
    ports = store.allocate_ports(sandbox_id)
    try:
        runtime.start_sandbox(sandbox_id, tpl, list(ports), envd_token, env_vars)
    except Exception as e:
        store.delete_sandbox(sandbox_id)
        log.exception("container start failed")
        return err(100005, f"沙箱启动失败: {e}", 500)

    features = runtime.features_for(tpl)
    if not runtime.wait_envd(ports[0], timeout_s=25.0):
        log.warning("envd health check timeout for %s (continuing)", sandbox_id)
    if "browser" in features and not runtime.wait_browser(ports[2], timeout_s=60.0):
        log.warning("browser health check timeout for %s (continuing)", sandbox_id)

    # P3: startup hooks (fail-closed on blocking failure)
    startup_hooks = json.loads(tpl.get("startup_hooks") or "[]")
    hook_state = {}
    if startup_hooks:
        log.info("running %d startup hooks for %s", len(startup_hooks), sandbox_id)
        res = runtime.run_startup_hooks(sandbox_id, startup_hooks)
        hook_state["startup"] = {
            "ran_at": time.time(),
            "all_passed": res["all_passed"],
            "blocking_failures": res["blocking_failures"],
            "results": res["results"],
        }
        if not res["all_passed"]:
            log.warning("startup hooks failed for %s (blocking=%s) — tearing down",
                        sandbox_id, res["blocking_failures"])
            runtime.remove_container(sandbox_id)
            store.delete_sandbox(sandbox_id)
            return err(100010,
                       f"启动钩子失败: {res['blocking_failures']}; sandbox 已回滚",
                       500)

    store.create_sandbox(sandbox_id, template_id, client_id, envd_token, list(ports), metadata,
                         f"sbx-{sandbox_id}", timeout, features=features,
                         owner=caller_identity(request)[0],
                         tenant=caller_identity(request)[1],
                         session_id=session_id)
    if hook_state:
        store.update_sandbox(sandbox_id, hook_state=json.dumps(hook_state))
    row = store.get_sandbox(sandbox_id)
    log.info("created sandbox %s ports=%s features=%s ttl=%ss", sandbox_id, ports, features, timeout)
    metrics.SANDBOX_CREATED.labels(template=template_id).inc()
    metrics.SANDBOX_ACTIVE.labels(state="running").inc()
    return JSONResponse(sandbox_json(row, with_token=True), status_code=201)


@app.get("/v2/sandboxes")
def list_sandboxes(request: Request):
    state_param = request.query_params.get("state")
    states = [s for s in state_param.split(",")] if state_param else ("running", "paused")
    owner, tenant = caller_identity(request)
    # P4 第六刀: when X-Session-Id is provided, scope the list to that session
    # only — other sessions' sandboxes are invisible. Without the header the
    # caller sees all their own sandboxes (legacy/admin view).
    session_param = request.headers.get("x-session-id", "").strip() or None
    rows = store.list_sandboxes(tuple(states),
                                owner=owner if ISOLATION_ENABLED else None,
                                tenant=tenant if ISOLATION_ENABLED else None,
                                session_id=session_param)
    return [sandbox_json(r, with_token=False) for r in rows]


@app.get("/sandboxes/{sandbox_id}")
def get_sandbox(sandbox_id: str, request: Request):
    row = store.get_sandbox(sandbox_id)
    if not row:
        return err(100003, f"沙箱不存在: {sandbox_id}", 404)
    deny = check_owner(request, row)
    if deny:
        return deny
    # E2B 语义：GET 单个沙箱需带 envdAccessToken，SDK connect 依赖它
    return sandbox_json(row, with_token=True)


@app.get("/sandboxes/{sandbox_id}/health")
def sandbox_health(sandbox_id: str, request: Request):
    """P1: health polling for the data-plane services inside one sandbox."""
    row = store.get_sandbox(sandbox_id)
    if not row:
        return err(100003, f"沙箱不存在: {sandbox_id}", 404)
    deny = check_owner(request, row)
    if deny:
        return deny
    import httpx

    def probe(port):
        if not port:
            return None
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=3.0)
            body = {}
            try:
                body = r.json()
            except Exception:
                pass
            ok = r.status_code == 200 and (body.get("ok", True) if isinstance(body, dict) else True)
            return {"port": port, "ok": ok, "detail": body}
        except Exception as e:
            return {"port": port, "ok": False, "error": str(e)}

    features = row.get("features") or "envd,jupyter"
    out = {
        "sandboxID": sandbox_id,
        "state": row["state"],
        "containerRunning": runtime.container_running(sandbox_id),
        "envd": probe(row["host_port_envd"]),
        "jupyter": probe(row["host_port_jupyter"]) if "jupyter" in features else None,
        "browser": probe(row.get("host_port_browser")) if "browser" in features else None,
    }
    out["ok"] = all(
        (v is None or v["ok"]) for k, v in out.items() if k in ("envd", "jupyter", "browser")
    )
    return out


def _resume_data_plane(row: dict, template: dict, timeout_s: float = 20.0, browser_timeout_s: float = 60.0):
    """Start or restore the container, wait for data-plane readiness."""
    mode = row.get("pause_mode") or "stop"
    features = row.get("features") or "envd,jupyter"
    if mode == "criu" and runtime.restore_container(row["sandbox_id"]):
        log.info("restored %s from CRIU checkpoint", row["sandbox_id"])
    else:
        if mode == "criu":
            log.warning("CRIU restore failed for %s, falling back to plain start", row["sandbox_id"])
        if not runtime.start_container(row["sandbox_id"], template):
            return None, "沙箱容器不存在"

    t0 = time.time()
    ok = runtime.wait_envd(row["host_port_envd"], timeout_s=timeout_s)
    log.info("resume %s: envd ready=%s in %.2fs (port %s)", row["sandbox_id"], ok, time.time() - t0,
             row["host_port_envd"])
    if not ok:
        return None, "沙箱恢复后 envd 未就绪"
    if "browser" in features and row.get("host_port_browser"):
        t1 = time.time()
        bok = runtime.wait_browser(row["host_port_browser"], timeout_s=browser_timeout_s)
        log.info("resume %s: browser ready=%s in %.2fs", row["sandbox_id"], bok, time.time() - t1)
    return True, None


@app.post("/sandboxes/{sandbox_id}/connect")
async def connect_sandbox(sandbox_id: str, request: Request):
    row = store.get_sandbox(sandbox_id)
    if not row:
        return err(100003, f"沙箱不存在: {sandbox_id}", 404)
    deny = check_owner(request, row)
    if deny:
        return deny
    tpl = store.get_template(row["template_code"])
    if row["state"] != "running":
        ok, msg = _resume_data_plane(row, tpl)
        if not ok:
            return err(100006, msg, 404 if "不存在" in msg else 503)
        store.update_sandbox(sandbox_id, state="running")
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    timeout = int(body.get("timeout") or 300)
    store.update_sandbox(sandbox_id, end_at=time.time() + timeout, last_activity=time.time())
    row = store.get_sandbox(sandbox_id)
    return sandbox_json(row, with_token=True)


@app.post("/sandboxes/{sandbox_id}/pause")
async def pause_sandbox(sandbox_id: str, request: Request):
    row = store.get_sandbox(sandbox_id)
    if not row:
        return err(100003, f"沙箱不存在: {sandbox_id}", 404)
    deny = check_owner(request, row)
    if deny:
        return deny
    if row["state"] == "paused":
        return Response(status_code=409)
    try:
        body = await request.json()
    except Exception:
        body = {}
    use_criu = str(body.get("criu", "auto")).lower() in ("1", "true", "auto", "yes")

    mode = "stop"
    if use_criu and runtime.criu_available():
        if runtime.checkpoint_container(sandbox_id):
            mode = "criu"
        else:
            log.warning("CRIU checkpoint failed for %s, using docker stop", sandbox_id)
    if mode == "stop":
        runtime.stop_container(sandbox_id)
    store.update_sandbox(sandbox_id, state="paused", pause_mode=mode)
    metrics.SANDBOX_ACTIVE.labels(state="running").dec()
    metrics.SANDBOX_ACTIVE.labels(state="paused").inc()
    return Response(status_code=204)


@app.post("/sandboxes/{sandbox_id}/resume")
async def resume_sandbox(sandbox_id: str, request: Request):
    row = store.get_sandbox(sandbox_id)
    if not row:
        return err(100003, f"沙箱不存在: {sandbox_id}", 404)
    deny = check_owner(request, row)
    if deny:
        return deny
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    timeout = int(body.get("timeout") or 300)
    tpl = store.get_template(row["template_code"])
    ok, msg = _resume_data_plane(row, tpl)
    if not ok:
        return err(100006, msg, 404 if "不存在" in msg else 503)
    store.update_sandbox(sandbox_id, state="running", end_at=time.time() + timeout,
                         last_activity=time.time())
    row = store.get_sandbox(sandbox_id)
    metrics.SANDBOX_ACTIVE.labels(state="paused").dec()
    metrics.SANDBOX_ACTIVE.labels(state="running").inc()
    return sandbox_json(row, with_token=True)


@app.delete("/sandboxes/{sandbox_id}")
def kill_sandbox(sandbox_id: str, request: Request):
    row = store.get_sandbox(sandbox_id)
    if not row:
        return Response(status_code=404)
    deny = check_owner(request, row)
    if deny:
        return deny
    prev_state = row.get("state", "running")
    runtime.remove_container(sandbox_id)
    store.delete_sandbox(sandbox_id)
    log.info("killed sandbox %s", sandbox_id)
    metrics.SANDBOX_DESTROYED.labels(reason="user").inc()
    if prev_state in ("running", "paused"):
        metrics.SANDBOX_ACTIVE.labels(state=prev_state).dec()
    return Response(status_code=204)


@app.post("/sandboxes/{sandbox_id}/timeout")
async def set_timeout(sandbox_id: str, request: Request):
    row = store.get_sandbox(sandbox_id)
    if not row:
        return err(100003, f"沙箱不存在: {sandbox_id}", 404)
    deny = check_owner(request, row)
    if deny:
        return deny
    # E2B 语义：paused 状态下 set_timeout 必须报错，SDK 才会回退去调 /resume
    if row["state"] != "running":
        return Response(status_code=409)
    body = await request.json()
    store.update_sandbox(sandbox_id, end_at=time.time() + int(body.get("timeout") or 300))
    return Response(status_code=204)


@app.post("/sandboxes/{sandbox_id}/refreshes")
async def refresh_sandbox(sandbox_id: str, request: Request):
    row = store.get_sandbox(sandbox_id)
    if not row:
        return err(100003, f"沙箱不存在: {sandbox_id}", 404)
    deny = check_owner(request, row)
    if deny:
        return deny
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    store.update_sandbox(sandbox_id, end_at=time.time() + int(body.get("timeout") or 300),
                         last_activity=time.time())
    return Response(status_code=204)


# ---------------- net policy (P2) ----------------

@app.get("/sandboxes/{sandbox_id}/netpolicy")
def get_netpolicy(sandbox_id: str, request: Request):
    row = store.get_sandbox(sandbox_id)
    if not row:
        return err(100003, f"沙箱不存在: {sandbox_id}", 404)
    deny = check_owner(request, row)
    if deny:
        return deny
    return netpolicy.status(sandbox_id)


@app.post("/sandboxes/{sandbox_id}/netpolicy")
async def set_netpolicy(sandbox_id: str, request: Request):
    row = store.get_sandbox(sandbox_id)
    if not row:
        return err(100003, f"沙箱不存在: {sandbox_id}", 404)
    deny = check_owner(request, row)
    if deny:
        return deny
    body = await request.json()
    ip = runtime.container_ip(sandbox_id)
    r = netpolicy.apply(sandbox_id, ip, body)
    if r.get("applied"):
        metrics.NETPOLICY_APPLIED.inc()
    return r


@app.post("/sandboxes/{sandbox_id}/netpolicy/refresh")
def refresh_netpolicy(sandbox_id: str, request: Request):
    """P3: manually trigger FQDN re-resolution (e.g. for CDN rotation)."""
    row = store.get_sandbox(sandbox_id)
    if not row:
        return err(100003, f"沙箱不存在: {sandbox_id}", 404)
    deny = check_owner(request, row)
    if deny:
        return deny
    r = netpolicy.refresh(sandbox_id)
    if r is None:
        return {"sandboxID": sandbox_id, "skipped": True,
                "reason": "no active allowlist (open/blocked mode or sandbox torn down)"}
    metrics.NETPOLICY_REFRESHED.inc()
    return r


@app.delete("/sandboxes/{sandbox_id}/netpolicy")
def del_netpolicy(sandbox_id: str, request: Request):
    row = store.get_sandbox(sandbox_id)
    if not row:
        return err(100003, f"沙箱不存在: {sandbox_id}", 404)
    deny = check_owner(request, row)
    if deny:
        return deny
    ip = runtime.container_ip(sandbox_id)
    return netpolicy.revoke(sandbox_id, ip)


@app.get("/sandboxes/{sandbox_id}/diag")
def sandbox_diag(sandbox_id: str, request: Request):
    """Per-sandbox diagnostic snapshot — point-in-time introspection.

    Query params:
      include    comma-separated subset of:
                 processes, stats, logs, connections, envd
                 (default: all five)
      logTail    int, default 100 (only used when 'logs' is in include)

    Sections are collected independently — a failure in one does not
    stop the others, it just shows up as {"error": "..."} in that slot.
    """
    row = store.get_sandbox(sandbox_id)
    if not row:
        return err(100003, f"沙箱不存在: {sandbox_id}", 404)
    deny = check_owner(request, row)
    if deny:
        return deny
    raw = request.query_params.get("include")
    include = [s.strip() for s in raw.split(",") if s.strip()] if raw else None
    try:
        log_tail = int(request.query_params.get("logTail", "100"))
    except ValueError:
        return err(100008, "logTail must be int", 400)
    # 记录 diagnostics 被请求的 section
    sections = include if include else list(diagnostics.ALL_SECTIONS)
    for s in sections:
        metrics.DIAG_CALLS.labels(section=s).inc()
    return diagnostics.gather(
        sandbox_id,
        include=include,
        log_tail=log_tail,
        host_port_envd=row.get("host_port_envd"),
    )


@app.post("/internal/auto-resume")
async def internal_auto_resume(request: Request):
    """P3 ingress keepalive: triggered by edge proxy when upstream connection
    fails. Synchronously restores the sandbox so the very next retry succeeds."""
    sandbox_id = request.query_params.get("sandbox")
    if not sandbox_id:
        return err(100004, "missing sandbox param", 400)
    row = store.get_sandbox(sandbox_id)
    if not row:
        return err(100003, f"sandbox not found: {sandbox_id}", 404)
    deny = check_owner(request, row)
    if deny:
        return deny
    if row["state"] == "running":
        return {"ok": True, "alreadyRunning": True}
    if row["state"] != "paused":
        return err(100005, f"sandbox state={row['state']}, cannot auto-resume", 409)
    tpl = store.get_template(row["template_code"])
    ok, msg = _resume_data_plane(row, tpl)
    if not ok:
        return err(100006, msg, 503)
    store.update_sandbox(sandbox_id, state="running", last_activity=time.time())
    log.info("auto-resumed sandbox %s via ingress keepalive trigger", sandbox_id)
    metrics.SANDBOX_ACTIVE.labels(state="paused").dec()
    metrics.SANDBOX_ACTIVE.labels(state="running").inc()
    return {"ok": True}


# ---------------- hooks (P3) ----------------

@app.get("/sandboxes/{sandbox_id}/hooks/status")
def sandbox_hook_status(sandbox_id: str, request: Request):
    row = store.get_sandbox(sandbox_id)
    if not row:
        return err(100003, f"沙箱不存在: {sandbox_id}", 404)
    deny = check_owner(request, row)
    if deny:
        return deny
    tpl = store.get_template(row["template_code"])
    hs = json.loads(row.get("hook_state") or "{}")
    return {
        "sandboxID": sandbox_id,
        "templateCode": row["template_code"],
        "configuredStartupHooks": json.loads(tpl.get("startup_hooks") or "[]") if tpl else [],
        "configuredPeriodicHooks": json.loads(tpl.get("periodic_hooks") or "[]") if tpl else [],
        "hookState": hs,
    }


# ---------------- templates ----------------

@app.post("/v3/templates")
async def create_template(request: Request):
    body = await request.json()
    name = body.get("name") or "template"
    image = body.get("image") or "sandbox/code-interpreter:v1"
    cpu = body.get("cpuCount") or 1
    mem = int(body.get("memoryMB") or 2048)
    disk = int(body.get("diskSizeMB") or 2048)
    envs = body.get("envVars") or {}
    browser = bool(body.get("browserEnabled"))
    net = body.get("networkPolicy") or {"mode": "open"}
    startup_hooks = body.get("startupHooks") or []
    periodic_hooks = body.get("periodicHooks") or []
    code = store.create_template(name, image, cpu, mem, disk, envs,
                                  browser_enabled=browser, network_policy=net,
                                  startup_hooks=startup_hooks, periodic_hooks=periodic_hooks)
    tpl = store.get_template(code)
    log.info("created template %s (browser=%s net=%s hooks_startup=%d periodic=%d)",
             code, tpl["browser_enabled"], tpl["network_policy"],
             len(startup_hooks), len(periodic_hooks))
    return JSONResponse({
        "templateCode": code, "templateID": code, "name": name, "image": image,
        "browserEnabled": bool(tpl["browser_enabled"]), "networkPolicy": json.loads(tpl["network_policy"]),
        "startupHooks": json.loads(tpl.get("startup_hooks") or "[]"),
        "periodicHooks": json.loads(tpl.get("periodic_hooks") or "[]"),
    }, status_code=201)


@app.get("/v2/templates")
def list_templates():
    out = []
    for t in store.list_templates():
        out.append({"templateCode": t["code"], "templateID": t["code"], "name": t["name"], "image": t["image"],
                    "cpuCount": t["cpu_count"], "memoryMB": t["memory_mb"], "version": t["version"],
                    "browserEnabled": bool(t.get("browser_enabled")),
                    "networkPolicy": json.loads(t.get("network_policy") or "{}"),
                    "startupHooks": json.loads(t.get("startup_hooks") or "[]"),
                    "periodicHooks": json.loads(t.get("periodic_hooks") or "[]")})
    return out


@app.get("/templates/{template_code}")
def get_template(template_code: str):
    t = store.get_template(template_code) or store.get_template_by_name(template_code)
    if not t:
        return err(100002, f"模版不存在: {template_code}", 404)
    return {"templateCode": t["code"], "templateID": t["code"], "name": t["name"], "image": t["image"],
            "cpuCount": t["cpu_count"], "memoryMB": t["memory_mb"], "diskSizeMB": t["disk_size_mb"],
            "version": t["version"], "browserEnabled": bool(t.get("browser_enabled")),
            "networkPolicy": json.loads(t.get("network_policy") or "{}"),
            "startupHooks": json.loads(t.get("startup_hooks") or "[]"),
            "periodicHooks": json.loads(t.get("periodic_hooks") or "[]")}


@app.get("/templates/{template_code}/hooks")
def get_template_hooks(template_code: str):
    t = store.get_template(template_code)
    if not t:
        return err(100002, f"模版不存在: {template_code}", 404)
    return {"templateCode": t["code"],
            "startupHooks": json.loads(t.get("startup_hooks") or "[]"),
            "periodicHooks": json.loads(t.get("periodic_hooks") or "[]")}


@app.put("/templates/{template_code}/hooks")
async def put_template_hooks(template_code: str, request: Request):
    t = store.get_template(template_code)
    if not t:
        return err(100002, f"模版不存在: {template_code}", 404)
    body = await request.json()
    startup_hooks = body.get("startupHooks")
    periodic_hooks = body.get("periodicHooks")
    if startup_hooks is None and periodic_hooks is None:
        return err(100004, "startupHooks / periodicHooks 至少传一个", 400)
    store.update_template_hooks(template_code,
                                startup_hooks=startup_hooks,
                                periodic_hooks=periodic_hooks)
    t = store.get_template(template_code)
    log.info("updated hooks template %s startup=%d periodic=%d",
             template_code, len(startup_hooks or []), len(periodic_hooks or []))
    return {"templateCode": t["code"],
            "startupHooks": json.loads(t.get("startup_hooks") or "[]"),
            "periodicHooks": json.loads(t.get("periodic_hooks") or "[]")}


@app.delete("/templates/{template_code}")
def delete_template(template_code: str):
    if not store.get_template(template_code):
        return err(100002, f"模版不存在: {template_code}", 404)
    if not store.delete_template(template_code):
        return err(100007, "模版下仍有运行中/暂停的实例，请先释放", 409)
    return Response(status_code=204)


@app.get("/templates/{template_code}/builds/{build_id}/status")
def build_status(template_code: str, build_id: str):
    b = store.get_build(template_code, build_id)
    if not b:
        return err(100003, "构建不存在", 404)
    return {"buildID": b["build_id"], "templateCode": b["template_code"], "status": b["status"], "logs": b["logs"]}


# ---------------- scheduler ----------------

@app.on_event("startup")
async def start_scheduler():
    import asyncio

    async def warmup():
        """Probe CRIU / iptables capability once in the background (P2)."""
        loop = asyncio.get_event_loop()
        try:
            criu = await loop.run_in_executor(None, runtime.criu_available)
            net = await loop.run_in_executor(None, netpolicy.supported)
            log.info("capability warmup: criu=%s netpolicy=%s", criu, net)
        except Exception:
            log.exception("warmup error")

    async def reaper():
        while True:
            try:
                now = time.time()
                for row in store.list_sandboxes(("running",)):
                    if row["end_at"] < now:
                        log.info("TTL reached, killing sandbox %s", row["sandbox_id"])
                        runtime.remove_container(row["sandbox_id"])
                        store.delete_sandbox(row["sandbox_id"])
            except Exception:
                log.exception("reaper error")
            await asyncio.sleep(15)

    async def periodic_hooks_loop():
        """P3: dispatch periodic hooks (best-effort) for all live sandboxes."""
        loop = asyncio.get_event_loop()
        while True:
            try:
                await loop.run_in_executor(None, _run_periodic_for_all)
            except Exception:
                log.exception("periodic hook loop error")
            await asyncio.sleep(HOOK_TICK_S)

    async def netpolicy_refresh_loop():
        """P3: re-resolve FQDN allowlist so CDN rotation doesn't blackhole
        sandbox egress. Default 5min, override via NETPOLICY_REFRESH_S."""
        loop = asyncio.get_event_loop()
        while True:
            try:
                refreshed = await loop.run_in_executor(None, netpolicy.refresh_all)
                if refreshed:
                    log.info("netpolicy refresh: %d sandboxes updated", len(refreshed))
            except Exception:
                log.exception("netpolicy refresh error")
            await asyncio.sleep(NETPOLICY_REFRESH_S)

    asyncio.create_task(warmup())
    asyncio.create_task(reaper())
    asyncio.create_task(periodic_hooks_loop())
    asyncio.create_task(netpolicy_refresh_loop())


def _run_periodic_for_all():
    """Synchronous periodic hook dispatcher (runs in thread pool)."""
    now = time.time()
    for row in store.list_sandboxes(("running",)):
        try:
            tpl = store.get_template(row["template_code"])
            if not tpl:
                continue
            periodic = json.loads(tpl.get("periodic_hooks") or "[]")
            if not periodic:
                continue
            hs = json.loads(row.get("hook_state") or "{}")
            pstate = hs.get("periodic", {}).get("hooks", {}) or {}
            res = runtime.run_periodic_hooks(row["sandbox_id"], periodic, pstate, now)
            if res["ran"]:
                hs.setdefault("periodic", {})
                hs["periodic"]["hooks"] = res["state"]
                hs["periodic"]["last_tick"] = now
                store.update_sandbox(row["sandbox_id"], hook_state=json.dumps(hs))
                log.info("periodic hooks ran on %s: %s", row["sandbox_id"], res["ran"])
        except Exception:
            log.exception("periodic hooks failed for %s", row.get("sandbox_id"))
