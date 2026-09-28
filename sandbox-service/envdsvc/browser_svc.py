"""Browser data-plane service (container port 3000).

Runs a headless Chromium with remote debugging on 127.0.0.1:9222 and exposes a
CDP-compatible HTTP/WS surface on :3000, so that:

    playwright: chromium.connect_over_cdp("wss://3000-<sandboxID>.<domain>/devtools/browser/<id>")
    puppeteer : puppeteer.connect({ browserWSEndpoint: "wss://3000-<id>.<domain>/devtools/browser/<id>" })

works straight through the TLS edge proxy.

Endpoints
---------
GET  /health                        -> {"ok":..,"browser":..,"cdpReady":..,"sessions":N,"maxSessions":N}
GET  /                              -> service info
GET  /json/version                  -> CDP discovery (webSocketDebuggerUrl rewritten to public host)
GET  /json/list                     -> targets (ws urls rewritten)
GET|PUT /json/new?url=...           -> open a new tab
GET|PUT|DELETE /json/close/<id>     -> close tab
WS   /devtools/{...}                -> transparent CDP websocket tunnel to 127.0.0.1:9222

# one-shot helpers (backward compatible)
POST /screenshot                    -> {"url":..,"fullPage":true,"waitMs":800} -> image/png
POST /content                       -> {"url":..,"waitMs":800} -> {"title","url","html","text"}

# long-lived sessions (multi-tab, concurrent, stateful)
POST /session/create                -> {"url":..,"viewport":{..},"userAgent":..} -> {"sessionId":..}
GET  /session/list                  -> [{"sessionId","url","createdAt","idleMs"}]
POST /session/{sid}/act             -> {"actions":[ ... ]} -> {"ok":true,"results":[...]}
GET  /session/{sid}/screenshot?fullPage=true&waitMs=0 -> image/png
GET  /session/{sid}/pdf             -> application/pdf
GET  /session/{sid}/content         -> {"title","url","html","text"}
POST /session/{sid}/close           -> {"closed":true}
POST /session/close-all             -> {"closed":N}

Session actions ({"type": ..., ...}):
  goto       {url, waitUntil: load|domcontentloaded|none, timeoutMs}
  click      {selector, index?, timeoutMs}
  fill       {selector, value, timeoutMs}
  type       {selector, text, delayMs, timeoutMs}
  press      {key, selector?}
  hover      {selector, timeoutMs}
  select     {selector, value}
  wait       {selector? | ms}
  scroll     {x?, y? | selector?}
  evaluate   {expression, awaitPromise?}
  screenshot {fullPage?, path?}        -> {"data": "<base64 png>"}
  pdf        {path?}                   -> {"data": "<base64 pdf>"}
  html       {}                        -> {"html": "..."}
  text       {}                        -> {"text": "..."}
  title      {}                        -> {"title": "..."}
  cookies    {}                        -> {"cookies": [...]}
  setCookie  {name, value, domain?, path?}
  clearCookies {}
  console    {}                        -> {"logs": [...]}  (collected since session start)
  close      {}
"""
import asyncio
import base64
import json
import logging
import os
import shutil
import subprocess
import time
import uuid
from collections import deque
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response

CHROMIUM = os.getenv("CHROMIUM_BIN") or shutil.which("chromium") or shutil.which("chromium-browser") or "/usr/bin/chromium"
CDP_PORT = int(os.getenv("CDP_PORT", "9222"))
CDP_HOST = "127.0.0.1"
ENVD_TOKEN = os.getenv("ENVD_TOKEN", "")
WS_SCHEME = os.getenv("BROWSER_WS_SCHEME", "wss")
PROFILE_DIR = "/tmp/chrome-profile"
MAX_SESSIONS = int(os.getenv("BROWSER_MAX_SESSIONS", "8"))
SESSION_IDLE_TTL = int(os.getenv("BROWSER_SESSION_TTL", "1800"))  # seconds

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("browser")

app = FastAPI(title="sandbox-browser", docs_url=None, redoc_url=None)
STARTED_AT = time.time()
SESSIONS: Dict[str, "Session"] = {}


# --------------------------------------------------------------------------
# chromium lifecycle
# --------------------------------------------------------------------------
def launch_chromium():
    if not os.path.exists(CHROMIUM):
        return False, f"chromium not found at {CHROMIUM}"
    cmd = [
        CHROMIUM,
        "--headless=new",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-background-networking",
        # keep background (non-focused) tabs fully alive for concurrent multi-tab sessions
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--remote-debugging-address=127.0.0.1",
        f"--remote-debugging-port={CDP_PORT}",
        "--remote-allow-origins=*",
        f"--user-data-dir={PROFILE_DIR}",
        "--window-size=1280,800",
        "about:blank",
    ]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception as e:  # pragma: no cover
        return False, str(e)
    return True, "launched"


def cdp_up():
    try:
        r = httpx.get(f"http://{CDP_HOST}:{CDP_PORT}/json/version", timeout=1.5)
        return r.status_code == 200
    except Exception:
        return False


def wait_cdp(timeout_s: float = 30.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if cdp_up():
            return True
        time.sleep(0.4)
    return False


def public_origin(request: Request) -> str:
    host = request.headers.get("host") or request.headers.get("x-forwarded-host") or "localhost:3000"
    return f"{WS_SCHEME}://{host}"


def rewrite_ws(obj, origin: str):
    """Replace ws://127.0.0.1:9222/... with the public origin."""
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
            if isinstance(v, str) and v.startswith("ws://"):
                rest = v.split("ws://", 1)[1]
                path = rest.split("/", 1)[1] if "/" in rest else ""
                obj[k] = f"{origin}/{path}"
            else:
                rewrite_ws(v, origin)
    elif isinstance(obj, list):
        for v in obj:
            rewrite_ws(v, origin)
    return obj


def check_token(request: Request) -> bool:
    if not ENVD_TOKEN:
        return True
    return request.headers.get("x-access-token") == ENVD_TOKEN


def deny():
    return JSONResponse({"code": 100001, "message": "invalid access token"}, status_code=401)


@app.on_event("startup")
def on_startup():
    ok, msg = launch_chromium()
    if not ok:
        app.state.chromium_error = msg
        return
    app.state.chromium_error = None
    app.state.cdp_ready = wait_cdp(30.0)
    asyncio.get_event_loop().create_task(_session_reaper())


@app.on_event("shutdown")
def on_shutdown():
    for s in list(SESSIONS.values()):
        try:
            s.sync_close()
        except Exception:
            pass


async def _session_reaper():
    """Close sessions idle beyond SESSION_IDLE_TTL."""
    while True:
        await asyncio.sleep(30)
        try:
            now = time.time()
            for sid, s in list(SESSIONS.items()):
                if now - s.last_used > SESSION_IDLE_TTL:
                    log.info("reaping idle session %s", sid)
                    await s.close()
                    SESSIONS.pop(sid, None)
        except Exception:
            log.exception("session reaper error")


@app.get("/health")
def health():
    # Always check live CDP status instead of caching at startup
    return {
        "ok": cdp_up(),
        "browser": os.path.exists(CHROMIUM),
        "cdpReady": cdp_up(),  # check live, not cached
        "cdpPort": CDP_PORT,
        "sessions": len(SESSIONS),
        "maxSessions": MAX_SESSIONS,
        "uptime": round(time.time() - STARTED_AT, 2),
    }


@app.get("/")
def index():
    return {
        "service": "sandbox-browser",
        "cdpPort": CDP_PORT,
        "ready": cdp_up(),
        "sessions": len(SESSIONS),
        "endpoints": ["/health", "/json/version", "/json/list", "/json/new",
                      "/session/create", "/session/list", "/session/{sid}/act",
                      "/session/{sid}/screenshot", "/session/{sid}/content",
                      "/screenshot", "/content"],
    }


def _cdp_get(path: str, params=None):
    r = httpx.get(f"http://{CDP_HOST}:{CDP_PORT}{path}", params=params, timeout=10)
    return r.status_code, r.text


@app.get("/json/version")
def json_version(request: Request):
    code, text = _cdp_get("/json/version")
    if code != 200:
        return Response(text, status_code=code, media_type="application/json")
    return rewrite_ws(json.loads(text), public_origin(request))


@app.get("/json/list")
def json_list(request: Request):
    code, text = _cdp_get("/json/list")
    if code != 200:
        return Response(text, status_code=code, media_type="application/json")
    return rewrite_ws(json.loads(text), public_origin(request))


def _cdp_new_target(url: str = "about:blank"):
    """Chromium >= 130 switched /json/new from GET to PUT; support both."""
    last = None
    for method in ("PUT", "GET"):
        try:
            r = httpx.request(method, f"http://{CDP_HOST}:{CDP_PORT}/json/new",
                              params={"url": url}, timeout=10)
            if r.status_code == 200:
                return r.json()
            last = f"{method} -> {r.status_code}"
        except Exception as e:
            last = repr(e)
    raise RuntimeError(f"cannot create CDP target ({last})")


@app.api_route("/json/new", methods=["GET", "PUT"])
def json_new(request: Request, url: str = "about:blank"):
    try:
        target = _cdp_new_target(url)
    except Exception as e:
        return JSONResponse({"code": 100005, "message": str(e)}, status_code=500)
    return rewrite_ws(target, public_origin(request))


def _cdp_close(target_id: str) -> bool:
    for method in ("GET", "PUT", "DELETE"):
        try:
            r = httpx.request(method, f"http://{CDP_HOST}:{CDP_PORT}/json/close/{target_id}", timeout=5)
            if r.status_code == 200:
                return True
        except Exception:
            pass
    return False


@app.api_route("/json/close/{target_id}", methods=["GET", "PUT", "DELETE"])
def json_close(target_id: str):
    return {"closed": _cdp_close(target_id), "targetId": target_id}


@app.websocket("/devtools/{path:path}")
async def devtools(ws: WebSocket, path: str):
    import websockets

    origin = ws.headers.get("origin", "")
    await ws.accept()
    try:
        async with websockets.connect(
            f"ws://{CDP_HOST}:{CDP_PORT}/devtools/{path}",
            max_size=None,
            ping_interval=None,
            additional_headers={"Origin": origin} if origin else None,
        ) as upstream:
            async def client_to_up():
                try:
                    while True:
                        msg = await ws.receive_text()
                        await upstream.send(msg)
                except (WebSocketDisconnect, RuntimeError):
                    pass
                except Exception:
                    pass

            async def up_to_client():
                try:
                    async for msg in upstream:
                        await ws.send_text(msg)
                except Exception:
                    pass

            t1 = asyncio.create_task(client_to_up())
            t2 = asyncio.create_task(up_to_client())
            done, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
    except Exception:
        pass
    finally:
        try:
            await ws.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# CDP session (long-lived, stateful, per-tab)
# --------------------------------------------------------------------------
class Session:
    def __init__(self, sid: str, ws, target_id: str, url: str):
        self.sid = sid
        self.ws = ws
        self.target_id = target_id
        self.url = url
        self.created_at = time.time()
        self.last_used = time.time()
        self._mid = 0
        self._lock = asyncio.Lock()
        self._inbox: asyncio.Queue = asyncio.Queue()
        self.events: deque = deque(maxlen=500)
        self.logs: deque = deque(maxlen=500)
        self._reader = asyncio.create_task(self._read_loop())

    async def _read_loop(self):
        try:
            async for raw in self.ws:
                try:
                    data = json.loads(raw)
                except Exception:
                    continue
                m = data.get("method")
                if m == "Runtime.consoleAPICalled":
                    try:
                        parts = [a.get("value", a.get("description", "")) for a in data["params"].get("args", [])]
                        self.logs.append({"level": data["params"].get("type", "log"),
                                          "text": " ".join(str(p) for p in parts)})
                    except Exception:
                        pass
                elif m == "Runtime.exceptionThrown":
                    try:
                        d = data["params"]["exceptionDetails"]
                        self.logs.append({"level": "error", "text": d.get("exception", {}).get("description", str(d))})
                    except Exception:
                        pass
                elif m == "Page.javascriptDialogOpening":
                    asyncio.create_task(self._safe_call("Page.handleJavaScriptDialog", {"accept": True}))
                if m:
                    self.events.append(data)
                else:
                    await self._inbox.put(data)
        except Exception:
            pass

    async def _safe_call(self, method, params=None):
        try:
            await self.call(method, params, timeout=5)
        except Exception:
            pass

    async def call(self, method: str, params=None, timeout: float = 20.0):
        async with self._lock:
            self._mid += 1
            mid = self._mid
            await self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
            deadline = time.time() + timeout
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise TimeoutError(f"cdp {method} timeout")
                data = await asyncio.wait_for(self._inbox.get(), timeout=remaining)
                if data.get("id") == mid:
                    if "error" in data:
                        raise RuntimeError(f"CDP {method}: {data['error'].get('message')}")
                    return data.get("result", {})
                # stale response, keep waiting

    async def wait_event(self, name: str, timeout: float = 20.0):
        deadline = time.time() + timeout
        seen = len(self.events)
        while time.time() < deadline:
            while seen < len(self.events):
                ev = self.events[seen]
                seen += 1
                if ev.get("method") == name:
                    return ev
            await asyncio.sleep(0.05)
        return None

    async def evaluate(self, expression: str, timeout: float = 20.0, await_promise: bool = False):
        r = await self.call("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": await_promise,
            "userGesture": True,
        }, timeout=timeout)
        exc = r.get("exceptionDetails")
        if exc:
            raise RuntimeError(exc.get("exception", {}).get("description") or json.dumps(exc)[:300])
        return r.get("result", {}).get("value")

    async def close(self):
        try:
            self._reader.cancel()
        except Exception:
            pass
        try:
            await self.ws.close()
        except Exception:
            pass
        try:
            _cdp_close(self.target_id)
        except Exception:
            pass

    def sync_close(self):
        try:
            self._reader.cancel()
        except Exception:
            pass
        _cdp_close(self.target_id)


async def _new_session(url: str, viewport: Optional[dict], user_agent: Optional[str]) -> Session:
    import websockets

    if len(SESSIONS) >= MAX_SESSIONS:
        raise RuntimeError(f"session limit reached ({MAX_SESSIONS}); close some sessions")
    target = _cdp_new_target(url or "about:blank")
    ws = await websockets.connect(target["webSocketDebuggerUrl"], max_size=None, ping_interval=None)
    sid = "s" + uuid.uuid4().hex[:12]
    s = Session(sid, ws, target["id"], url or "about:blank")

    await s.call("Page.enable", timeout=10)
    await s.call("Runtime.enable", timeout=10)
    await s.call("Log.enable", timeout=10)
    try:
        await s.call("Network.enable", timeout=10)
    except Exception:
        pass
    vp = viewport or {"width": 1280, "height": 800}
    try:
        await s.call("Emulation.setDeviceMetricsOverride", {
            "width": int(vp.get("width", 1280)),
            "height": int(vp.get("height", 800)),
            "deviceScaleFactor": float(vp.get("deviceScaleFactor", 1)),
            "mobile": bool(vp.get("mobile", False)),
        }, timeout=10)
    except Exception:
        pass
    if user_agent:
        try:
            await s.call("Network.setUserAgentOverride", {"userAgent": user_agent}, timeout=10)
        except Exception:
            pass
    # /json/new?url= 在 headless=new 下不一定真正导航，显式 navigate 兜底
    if url and url != "about:blank":
        try:
            await s.call("Page.navigate", {"url": url}, timeout=15)
            await s.wait_event("Page.loadEventFired", timeout=15)
        except Exception:
            log.warning("initial navigate to %s failed/timed out (continuing)", url)
    return s


def _sel(sel: str) -> str:
    return json.dumps(sel)  # safe JS string literal


async def _wait_selector(s: Session, selector: str, timeout_ms: int):
    expr = (
        "(async()=>{const t=Date.now()+__T__;"
        "while(Date.now()<t){const e=document.querySelector(__S__);if(e)return true;"
        "await new Promise(r=>setTimeout(r,50));}return false;})()"
    ).replace("__T__", str(int(timeout_ms))).replace("__S__", _sel(selector))
    ok = await s.evaluate(expr, timeout=timeout_ms / 1000 + 10, await_promise=True)
    if not ok:
        raise TimeoutError(f"selector not found: {selector}")


async def _ensure_selector(s: Session, selector: str, timeout_ms: int = 10000):
    if selector:
        await _wait_selector(s, selector, timeout_ms)


async def _click_selector(s: Session, selector: str, index: int = 0, timeout_ms: int = 10000):
    await _ensure_selector(s, selector, timeout_ms)
    box = await s.evaluate(
        ("(()=>{const els=document.querySelectorAll(__S__);const e=els[__I__];if(!e)return null;"
         "e.scrollIntoView({block:'center',inline:'center'});const r=e.getBoundingClientRect();"
         "return {x:r.x+r.width/2,y:r.y+r.height/2};})()")
        .replace("__S__", _sel(selector)).replace("__I__", str(index)),
        timeout=15)
    if not box:
        raise RuntimeError(f"element not visible: {selector}")
    await s.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": box["x"], "y": box["y"],
                                              "button": "left", "clickCount": 1})
    await s.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": box["x"], "y": box["y"],
                                              "button": "left", "clickCount": 1})


async def _shot(s: Session, full_page: bool = False) -> bytes:
    r = await s.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": bool(full_page)},
                     timeout=30)
    return base64.b64decode(r["data"])


async def _pdf(s: Session) -> bytes:
    r = await s.call("Page.printToPDF", {"printBackground": True, "format": "A4"}, timeout=40)
    return base64.b64decode(r["data"])


async def _run_action(s: Session, a: dict) -> Any:
    t = (a.get("type") or "").lower()
    timeout_ms = int(a.get("timeoutMs") or 10000)

    if t == "goto":
        url = a["url"]
        until = (a.get("waitUntil") or "load").lower()
        if until != "none":
            await s.call("Page.enable", timeout=5)
        await s.call("Page.navigate", {"url": url}, timeout=max(10, timeout_ms / 1000))
        if until == "load":
            await s.wait_event("Page.loadEventFired", timeout=max(5, timeout_ms / 1000))
        elif until == "domcontentloaded":
            await s.wait_event("Page.domContentLoadedEventFired", timeout=max(5, timeout_ms / 1000))
        if a.get("waitMs"):
            await asyncio.sleep(int(a["waitMs"]) / 1000)
        s.url = url
        return {"url": url}

    if t == "click":
        await _click_selector(s, a["selector"], int(a.get("index") or 0), timeout_ms)
        return {"clicked": a["selector"]}

    if t == "fill":
        await _ensure_selector(s, a["selector"], timeout_ms)
        await s.evaluate(
            ("(()=>{const e=document.querySelector(__S__);if(!e)throw new Error('no element');"
             "const proto=Object.getPrototypeOf(e);const d=Object.getOwnPropertyDescriptor(proto,'value');"
             "if(d&&d.set){d.set.call(e,__V__);}else{e.value=__V__;}"
             "e.dispatchEvent(new Event('input',{bubbles:true}));"
             "e.dispatchEvent(new Event('change',{bubbles:true}));return true;})()")
            .replace("__S__", _sel(a["selector"])).replace("__V__", _sel(a.get("value", ""))),
            timeout=15)
        return {"filled": a["selector"]}

    if t == "type":
        await _ensure_selector(s, a["selector"], timeout_ms)
        await s.call("Input.insertText", {"text": a.get("text", "")}, timeout=10)
        return {"typed": a["selector"]}

    if t == "press":
        sel = a.get("selector")
        if sel:
            await _ensure_selector(s, sel, timeout_ms)
            await s.evaluate(f"document.querySelector({_sel(sel)}).focus()", timeout=10)
        key = a.get("key", "Enter")
        # dispatch raw key event then a char for text keys
        await s.call("Input.dispatchKeyEvent", {"type": "keyDown", "key": key, "code": key,
                                                "windowsVirtualKeyCode": ord(key) if len(key) == 1 else 13})
        await s.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": key, "code": key,
                                                "windowsVirtualKeyCode": ord(key) if len(key) == 1 else 13})
        return {"pressed": key}

    if t == "hover":
        await _ensure_selector(s, a["selector"], timeout_ms)
        box = await s.evaluate(
            ("(()=>{const e=document.querySelector(__S__);if(!e)return null;const r=e.getBoundingClientRect();"
             "return {x:r.x+r.width/2,y:r.y+r.height/2};})()").replace("__S__", _sel(a["selector"])), timeout=15)
        if not box:
            raise RuntimeError("element not visible")
        await s.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": box["x"], "y": box["y"]})
        return {"hovered": a["selector"]}

    if t == "select":
        await _ensure_selector(s, a["selector"], timeout_ms)
        await s.evaluate(
            ("(()=>{const e=document.querySelector(__S__);if(!e)throw new Error('no element');"
             "e.value=__V__;e.dispatchEvent(new Event('change',{bubbles:true}));return e.value;})()")
            .replace("__S__", _sel(a["selector"])).replace("__V__", _sel(a.get("value", ""))), timeout=15)
        return {"selected": a.get("value")}

    if t == "wait":
        if a.get("selector"):
            await _wait_selector(s, a["selector"], timeout_ms)
            return {"found": a["selector"]}
        await asyncio.sleep(int(a.get("ms") or 500) / 1000)
        return {"waited": a.get("ms") or 500}

    if t == "scroll":
        if a.get("selector"):
            await s.evaluate(
                f"document.querySelector({_sel(a['selector'])}).scrollIntoView({{block:'center'}})", timeout=10)
        else:
            await s.evaluate(f"window.scrollTo({int(a.get('x') or 0)},{int(a.get('y') or 0)})", timeout=10)
        return {"scrolled": True}

    if t in ("evaluate", "eval"):
        v = await s.evaluate(a["expression"], timeout=max(10, timeout_ms / 1000),
                             await_promise=bool(a.get("awaitPromise")))
        return {"value": v}

    if t == "screenshot":
        png = await _shot(s, bool(a.get("fullPage")))
        return {"bytes": len(png), "data": base64.b64encode(png).decode()}

    if t == "pdf":
        pdf = await _pdf(s)
        return {"bytes": len(pdf), "data": base64.b64encode(pdf).decode()}

    if t == "html":
        return {"html": await s.evaluate("document.documentElement.outerHTML", timeout=20)}

    if t == "text":
        return {"text": await s.evaluate("document.body?document.body.innerText:''", timeout=20)}

    if t == "title":
        return {"title": await s.evaluate("document.title", timeout=10)}

    if t == "url":
        return {"url": await s.evaluate("location.href", timeout=10)}

    if t == "cookies":
        url = await s.evaluate("location.href", timeout=10)
        r = await s.call("Network.getCookies", {"urls": [url]}, timeout=15)
        return {"cookies": r.get("cookies", [])}

    if t == "setcookie":
        p = {"name": a["name"], "value": a.get("value", ""), "path": a.get("path", "/")}
        if a.get("domain"):
            p["domain"] = a["domain"]
        else:
            # url 形式对 IP/localhost 更稳（domain 形式对 IP 常被拒）
            p["url"] = await s.evaluate("location.href", timeout=10)
        ok = await s.call("Network.setCookie", p, timeout=15)
        return {"set": bool(ok.get("success", True))}

    if t == "clearcookies":
        await s.call("Network.clearBrowserCookies", timeout=15)
        return {"cleared": True}

    if t == "console":
        return {"logs": list(s.logs)}

    if t == "close":
        await s.close()
        SESSIONS.pop(s.sid, None)
        return {"closed": True}

    raise ValueError(f"unknown action type: {t!r}")


# --------------------------------------------------------------------------
# session HTTP API
# --------------------------------------------------------------------------
@app.post("/session/create")
async def session_create(request: Request):
    if not check_token(request):
        return deny()
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    try:
        s = await _new_session(body.get("url"), body.get("viewport"), body.get("userAgent"))
    except Exception as e:
        return JSONResponse({"code": 100010, "message": str(e)}, status_code=500)
    SESSIONS[s.sid] = s
    return {"sessionId": s.sid, "targetId": s.target_id, "url": s.url}


@app.get("/session/list")
def session_list(request: Request):
    if not check_token(request):
        return deny()
    now = time.time()
    return {"sessions": [{"sessionId": s.sid, "url": s.url,
                          "createdAt": round(s.created_at, 2),
                          "idleMs": int((now - s.last_used) * 1000)} for s in SESSIONS.values()],
            "count": len(SESSIONS), "maxSessions": MAX_SESSIONS}


@app.post("/session/close-all")
async def session_close_all(request: Request):
    if not check_token(request):
        return deny()
    n = 0
    for sid in list(SESSIONS):
        s = SESSIONS.pop(sid, None)
        if s:
            await s.close()
            n += 1
    return {"closed": n}


@app.post("/session/{sid}/act")
async def session_act(sid: str, request: Request):
    if not check_token(request):
        return deny()
    s = SESSIONS.get(sid)
    if s is None:
        return JSONResponse({"code": 100011, "message": "session not found"}, status_code=404)
    body = await request.json()
    actions = body.get("actions") or []
    if isinstance(actions, dict):
        actions = [actions]
    s.last_used = time.time()
    results = []
    for a in actions:
        try:
            results.append({"ok": True, "type": a.get("type"), "result": await _run_action(s, a)})
        except Exception as e:
            results.append({"ok": False, "type": a.get("type"), "error": str(e)[:400]})
            if body.get("stopOnError", True):
                break
    s.last_used = time.time()
    return {"ok": all(r["ok"] for r in results), "sessionId": sid, "results": results}


@app.get("/session/{sid}/screenshot")
async def session_screenshot(sid: str, request: Request, fullPage: bool = False, waitMs: int = 0):
    if not check_token(request):
        return deny()
    s = SESSIONS.get(sid)
    if s is None:
        return JSONResponse({"code": 100011, "message": "session not found"}, status_code=404)
    s.last_used = time.time()
    if waitMs:
        await asyncio.sleep(waitMs / 1000)
    try:
        return Response(await _shot(s, fullPage), media_type="image/png")
    except Exception as e:
        return JSONResponse({"code": 100012, "message": str(e)}, status_code=500)


@app.get("/session/{sid}/pdf")
async def session_pdf(sid: str, request: Request):
    if not check_token(request):
        return deny()
    s = SESSIONS.get(sid)
    if s is None:
        return JSONResponse({"code": 100011, "message": "session not found"}, status_code=404)
    s.last_used = time.time()
    try:
        return Response(await _pdf(s), media_type="application/pdf")
    except Exception as e:
        return JSONResponse({"code": 100012, "message": str(e)}, status_code=500)


@app.get("/session/{sid}/content")
async def session_content(sid: str, request: Request):
    if not check_token(request):
        return deny()
    s = SESSIONS.get(sid)
    if s is None:
        return JSONResponse({"code": 100011, "message": "session not found"}, status_code=404)
    s.last_used = time.time()
    try:
        return {
            "sessionId": sid,
            "title": await s.evaluate("document.title", timeout=10),
            "url": await s.evaluate("location.href", timeout=10),
            "html": await s.evaluate("document.documentElement.outerHTML", timeout=20),
            "text": await s.evaluate("document.body?document.body.innerText:''", timeout=20),
        }
    except Exception as e:
        return JSONResponse({"code": 100012, "message": str(e)}, status_code=500)


@app.post("/session/{sid}/close")
async def session_close(sid: str, request: Request):
    if not check_token(request):
        return deny()
    s = SESSIONS.pop(sid, None)
    if s is None:
        return JSONResponse({"code": 100011, "message": "session not found"}, status_code=404)
    await s.close()
    return {"closed": True, "sessionId": sid}


# --------------------------------------------------------------------------
# one-shot helpers (kept for backward compatibility)
# --------------------------------------------------------------------------
async def _cdp_session(url: str, wait_ms: int = 800, timeout_s: float = 20.0):
    import websockets

    target = _cdp_new_target("about:blank")
    ws = await websockets.connect(target["webSocketDebuggerUrl"], max_size=None, ping_interval=None)
    s = Session("tmp" + uuid.uuid4().hex[:8], ws, target["id"], url)
    await s.call("Page.enable", timeout=10)
    if url and url != "about:blank":
        await s.call("Page.navigate", {"url": url}, timeout=max(10, timeout_s))
        try:
            await s.wait_event("Page.loadEventFired", timeout=max(10, min(timeout_s, 30)))
        except Exception:
            log.warning("loadEventFired timeout for %s (continuing)", url)
    if wait_ms:
        await asyncio.sleep(wait_ms / 1000)
    return s


@app.post("/screenshot")
async def screenshot(request: Request):
    if not check_token(request):
        return deny()
    body = await request.json()
    url = body.get("url") or "about:blank"
    full_page = bool(body.get("fullPage", False))
    wait_ms = int(body.get("waitMs") or 800)
    s = await _cdp_session(url, wait_ms=wait_ms)
    try:
        return Response(await _shot(s, full_page), media_type="image/png")
    except Exception as e:
        return JSONResponse({"code": 100012, "message": str(e)}, status_code=500)
    finally:
        await s.close()


@app.post("/content")
async def content(request: Request):
    if not check_token(request):
        return deny()
    body = await request.json()
    url = body.get("url") or "about:blank"
    wait_ms = int(body.get("waitMs") or 800)
    s = await _cdp_session(url, wait_ms=wait_ms)
    try:
        return {
            "title": await s.evaluate("document.title", timeout=10),
            "url": await s.evaluate("location.href", timeout=10),
            "html": await s.evaluate("document.documentElement.outerHTML", timeout=20),
            "text": await s.evaluate("document.body?document.body.innerText:''", timeout=20),
        }
    except Exception as e:
        return JSONResponse({"code": 100012, "message": str(e)}, status_code=500)
    finally:
        await s.close()
