"""P1 smoke: browser / all-in-one sandbox.

Verifies:
  1. templates for browser + all-in-one exist
  2. sandbox creation exposes container port 3000 and /health reports Chromium ready
  3. CDP discovery endpoint rewrites webSocketDebuggerUrl to the public TLS host
  4. Playwright connects over CDP through the TLS edge proxy (wss tunnelling)
  5. the /content + /screenshot convenience endpoints work
  6. an all-in-one sandbox still serves run_code (code interpreter parity)
"""
import json
import os
import sys
import time

import httpx
import os
SBX_API_KEY = os.environ.get("SBX_API_KEY", "")
E2B_KEY = os.environ.get("SBX_E2B_KEY", "")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["SSL_CERT_FILE"] = os.path.join(ROOT, "certs", "ca.pem")
os.environ["NODE_EXTRA_CA_CERTS"] = os.path.join(ROOT, "certs", "ca.pem")
# Playwright's node driver picks up the machine-wide HTTP proxy, which cannot reach
# the private sandbox domain -> strip proxy env for this process.
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"

API = os.environ.get("SBX_API_URL", "http://192.168.2.162:8902")
KEY = os.environ.get("SBX_API_KEY", "")
DOMAIN = os.environ.get("SBX_DOMAIN", "192.168.2.162.nip.io")
HEADERS = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}

FAILS = []


def check(name, cond, detail=""):
    print(f"{'OK  ' if cond else 'FAIL'}: {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def pick_template(preferred):
    r = httpx.get(f"{API}/v2/templates", headers=HEADERS, timeout=10)
    r.raise_for_status()
    tpls = r.json()
    for t in tpls:
        if t.get("name") == preferred:
            return t
    for t in tpls:
        if t.get("browserEnabled"):
            return t
    raise SystemExit(f"no browser-capable template found: {tpls}")


def wait_health(sandbox_id, timeout=90):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        r = httpx.get(f"{API}/sandboxes/{sandbox_id}/health", headers=HEADERS, timeout=10)
        last = r.json()
        if last.get("ok"):
            return last
        time.sleep(2)
    return last


def cleanup_all():
    """Test hygiene: release every live sandbox so port/memory budget is free."""
    r = httpx.get(f"{API}/v2/sandboxes", headers=HEADERS, timeout=10)
    for s in r.json():
        try:
            httpx.delete(f"{API}/sandboxes/{s['sandboxID']}", headers=HEADERS, timeout=20)
        except Exception:
            pass


def main():
    flavour = sys.argv[1] if len(sys.argv) > 1 else "all-in-one"
    cleanup_all()
    tpl = pick_template(flavour)
    print("template:", tpl["templateCode"], tpl["name"], "browser=", tpl.get("browserEnabled"))
    check("template has browser enabled", bool(tpl.get("browserEnabled")))

    r = httpx.post(f"{API}/sandboxes", headers=HEADERS,
                   json={"templateID": tpl["templateCode"], "timeout": 900}, timeout=300)
    r.raise_for_status()
    sbx = r.json()
    sid = sbx["sandboxID"]
    token = sbx["envdAccessToken"]
    print("sandbox:", sid, "features:", sbx.get("features"), "browserPort:", sbx.get("browserPort"))
    check("sandbox reports browser port 3000", sbx.get("browserPort") == 3000)

    # ---------- 1. health polling ----------
    health = wait_health(sid, timeout=180)
    print("health:", json.dumps(health, ensure_ascii=False)[:400])
    check("envd healthy", bool(health.get("envd", {}).get("ok")))
    check("browser healthy", bool(health.get("browser", {}).get("ok")), str(health.get("browser")))

    host = f"3000-{sid}.{DOMAIN}"
    base = f"https://{host}"

    # ---------- 2. CDP discovery ----------
    r = httpx.get(f"{base}/json/version", timeout=20)
    check("GET /json/version 200", r.status_code == 200, str(r.status_code))
    ver = r.json()
    ws = ver.get("webSocketDebuggerUrl", "")
    print("webSocketDebuggerUrl:", ws)
    check("webSocketDebuggerUrl rewritten to public host", ws.startswith(f"wss://{host}/devtools/browser/"))

    targets = httpx.get(f"{base}/json/list", timeout=20).json()
    check("GET /json/list returns targets", isinstance(targets, list) and len(targets) > 0, f"{len(targets)} targets")

    # ---------- 3. Playwright over CDP ----------
    from playwright.sync_api import sync_playwright

    # feed Playwright the resolved ws endpoint directly (its http-endpoint
    # auto-discovery mis-parses the <port>-<id>.<domain> host form)
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(ws)
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.new_page()
        page.goto("data:text/html,<h1>hello-browser-sandbox</h1><p>P1 ok</p>")
        title = page.title()
        h1 = page.inner_text("h1")
        shot = page.screenshot()
        print("playwright ->", repr(h1), "shot bytes:", len(shot))
        check("playwright connected over CDP", True)
        check("page rendered heading", h1 == "hello-browser-sandbox", h1)
        check("screenshot non-empty", len(shot) > 1000, f"{len(shot)} bytes")
        page.close()
        browser.close()

    # ---------- 4. convenience endpoints ----------
    r = httpx.post(f"{base}/content", headers={"X-Access-Token": token},
                   json={"url": "data:text/html,<h1>conv-api</h1><p>body text</p>"}, timeout=30)
    check("POST /content 200", r.status_code == 200, str(r.status_code))
    body = r.json()
    check("/content returns parsed text", "body text" in body.get("text", ""), str(body.get("text"))[:80])

    r = httpx.post(f"{base}/screenshot", headers={"X-Access-Token": token},
                   json={"url": "data:text/html,<h1>shot</h1>", "fullPage": True}, timeout=40)
    check("POST /screenshot returns png", r.status_code == 200 and r.content[:4] == b"\x89PNG",
          f"rc={r.status_code} bytes={len(r.content)}")

    # ---------- 5. code interpreter parity (all-in-one) ----------
    if "jupyter" in (sbx.get("features") or ""):
        from e2b_code_interpreter import Sandbox as CodeSandbox
        cs = CodeSandbox.connect(sid, api_url=API, api_key=E2B_KEY, domain=DOMAIN)
        res = cs.run_code("import sys; print('py', sys.version_info.minor)")
        text = "".join(o if isinstance(o, str) else o.text for o in (res.logs.stdout or []))
        check("run_code works on all-in-one", "py" in text, text.strip())
        cs.kill()
    else:
        httpx.delete(f"{API}/sandboxes/{sid}", headers=HEADERS, timeout=30)
        check("sandbox killed", True)

    print("\n" + ("P1 BROWSER SMOKE PASSED" if not FAILS else f"P1 FAILED: {FAILS}"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
