"""Sandbox service control plane — E2B compatible API (reference: Aliyun Bailian Sandbox).

P1: browser / all-in-one templates (container port 3000) + /health polling.
P2: network allowlist per template, CRIU-aware pause/resume, admission control (quota).
"""
import json
import logging
import os
import time
from datetime import datetime, timezone

import netpolicy
import diagnostics
import metrics
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

import store
import runtime

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("control-plane")

API_KEYS = [k.strip() for k in os.getenv("API_KEYS", "").split(",") if k.strip()]
SANDBOX_DOMAIN = os.getenv("SANDBOX_DOMAIN", "192.168.2.162.nip.io")
ENVD_VERSION = "0.7.0"
MAX_SANDBOXES = int(os.getenv("MAX_SANDBOXES", "24"))
MAX_MEMORY_MB = int(os.getenv("MAX_MEMORY_MB", "6144"))
HOOK_TICK_S = int(os.getenv("HOOK_TICK_S", "30"))
NETPOLICY_REFRESH_S = int(os.getenv("NETPOLICY_REFRESH_S", "300"))  # 5min

app = FastAPI(title="sandbox-service", docs_url=None, redoc_url=None)
store.init_db()
runtime.ensure_network()


def err(code: int, message: str, status: int):
    rid = f"req-{int(time.time()*1000)}"
    return JSONResponse({"code": code, "message": message, "requestID": rid}, status_code=status)


@app.middleware("http")
async def auth(request: Request, call_next):
    path = request.url.path
    if path in ("/health", "/metrics", "/") or path.startswith("/internal"):
        return await call_next(request)
    # Two credential styles are accepted:
    #   Authorization: Bearer <key>   (Bailian / our REST convention)
    #   X-API-KEY: <key>              (what the official e2b SDK actually sends)
    authz = request.headers.get("authorization", "")
    token = authz[7:] if authz.lower().startswith("bearer ") else ""
    if not token:
        token = request.headers.get("x-api-key", "")
    if token not in API_KEYS:
        return err(100001, "API Key 无效", 401)
    return await call_next(request)


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


@app.post("/sandboxes")
async def create_sandbox(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    template_id = body.get("templateID") or body.get("template_id")
    if not template_id:
        return err(100004, "参数缺失: templateID", 400)
    tpl = store.get_template(template_id)
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
                         f"sbx-{sandbox_id}", timeout, features=features)
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
    rows = store.list_sandboxes(tuple(states))
    return [sandbox_json(r, with_token=False) for r in rows]


@app.get("/sandboxes/{sandbox_id}")
def get_sandbox(sandbox_id: str):
    row = store.get_sandbox(sandbox_id)
    if not row:
        return err(100003, f"沙箱不存在: {sandbox_id}", 404)
    # E2B 语义：GET 单个沙箱需带 envdAccessToken，SDK connect 依赖它
    return sandbox_json(row, with_token=True)


@app.get("/sandboxes/{sandbox_id}/health")
def sandbox_health(sandbox_id: str):
    """P1: health polling for the data-plane services inside one sandbox."""
    row = store.get_sandbox(sandbox_id)
    if not row:
        return err(100003, f"沙箱不存在: {sandbox_id}", 404)
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

    out = {
        "sandboxID": sandbox_id,
        "state": row["state"],
        "containerRunning": runtime.container_running(sandbox_id),
        "envd": probe(row["host_port_envd"]),
        "jupyter": probe(row["host_port_jupyter"]),
        "browser": probe(row.get("host_port_browser")),
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
def kill_sandbox(sandbox_id: str):
    row = store.get_sandbox(sandbox_id)
    if not row:
        return Response(status_code=404)
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
def get_netpolicy(sandbox_id: str):
    row = store.get_sandbox(sandbox_id)
    if not row:
        return err(100003, f"沙箱不存在: {sandbox_id}", 404)
    return netpolicy.status(sandbox_id)


@app.post("/sandboxes/{sandbox_id}/netpolicy")
async def set_netpolicy(sandbox_id: str, request: Request):
    row = store.get_sandbox(sandbox_id)
    if not row:
        return err(100003, f"沙箱不存在: {sandbox_id}", 404)
    body = await request.json()
    ip = runtime.container_ip(sandbox_id)
    r = netpolicy.apply(sandbox_id, ip, body)
    if r.get("applied"):
        metrics.NETPOLICY_APPLIED.inc()
    return r


@app.post("/sandboxes/{sandbox_id}/netpolicy/refresh")
def refresh_netpolicy(sandbox_id: str):
    """P3: manually trigger FQDN re-resolution (e.g. for CDN rotation)."""
    row = store.get_sandbox(sandbox_id)
    if not row:
        return err(100003, f"沙箱不存在: {sandbox_id}", 404)
    r = netpolicy.refresh(sandbox_id)
    if r is None:
        return {"sandboxID": sandbox_id, "skipped": True,
                "reason": "no active allowlist (open/blocked mode or sandbox torn down)"}
    metrics.NETPOLICY_REFRESHED.inc()
    return r


@app.delete("/sandboxes/{sandbox_id}/netpolicy")
def del_netpolicy(sandbox_id: str):
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
def sandbox_hook_status(sandbox_id: str):
    row = store.get_sandbox(sandbox_id)
    if not row:
        return err(100003, f"沙箱不存在: {sandbox_id}", 404)
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
    t = store.get_template(template_code)
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
