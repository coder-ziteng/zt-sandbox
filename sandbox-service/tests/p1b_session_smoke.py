"""P1+ smoke: browser SESSIONS (long-lived, multi-tab, concurrent) + stress.

Verifies the session API added on top of the CDP browser service:
  1. sandbox with browser feature -> /health reports cdpReady
  2. POST /session/create              -> sessionId
  3. act: goto -> fill -> click -> evaluate  (stateful page interaction)
  4. act: screenshot / html / text / title
  5. act: cookies / setCookie / console
  6. multi-session in ONE sandbox (concurrent tabs, isolated state)
  7. multi-sandbox concurrency (N sandboxes in parallel)
  8. session reuse keeps state across separate HTTP calls (long session)
  9. /session/{sid}/content + /session/{sid}/pdf
 10. close + close-all

Usage:
    python tests/p1b_session_smoke.py [browser|all-in-one] [concurrency]
"""
import concurrent.futures as cf
import json
import os
import sys
import time

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["SSL_CERT_FILE"] = os.path.join(ROOT, "certs", "ca.pem")
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"

API = "http://<internal-host>:8902"
KEY = "<dev-key-redacted>"
DOMAIN = "<internal-host>.nip.io"
HEADERS = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}

FAILS = []


def check(name, cond, detail=""):
    print(f"{'OK  ' if cond else 'FAIL'}: {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def pick_template(preferred):
    r = httpx.get(f"{API}/v2/templates", headers=HEADERS, timeout=10)
    r.raise_for_status()
    for t in r.json():
        if t.get("name") == preferred:
            return t
    for t in r.json():
        if t.get("browserEnabled"):
            return t
    raise SystemExit("no browser template")


def create(tpl_code, timeout=900):
    r = httpx.post(f"{API}/sandboxes", headers=HEADERS,
                   json={"templateID": tpl_code, "timeout": timeout}, timeout=180)
    r.raise_for_status()
    return r.json()


def kill(sid):
    try:
        httpx.delete(f"{API}/sandboxes/{sid}", headers=HEADERS, timeout=30)
    except Exception:
        pass


def wait_health(sid, timeout=180):
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        r = httpx.get(f"{API}/sandboxes/{sid}/health", headers=HEADERS, timeout=15)
        last = r.json()
        if last.get("ok"):
            return last
        time.sleep(2)
    return last


class Browser:
    def __init__(self, sandbox_id, token):
        self.base = f"https://3000-{sandbox_id}.{DOMAIN}"
        self.h = {"X-Access-Token": token, "Content-Type": "application/json"}

    def health(self):
        return httpx.get(f"{self.base}/health", headers=self.h, timeout=15).json()

    def new_session(self, url="about:blank", viewport=None):
        r = httpx.post(f"{self.base}/session/create", headers=self.h,
                       json={"url": url, "viewport": viewport or {"width": 1280, "height": 800}}, timeout=60)
        r.raise_for_status()
        return r.json()["sessionId"]

    def act(self, sid, actions, stop_on_error=True):
        r = httpx.post(f"{self.base}/session/{sid}/act", headers=self.h,
                       json={"actions": actions, "stopOnError": stop_on_error}, timeout=180)
        r.raise_for_status()
        return r.json()

    def content(self, sid):
        return httpx.get(f"{self.base}/session/{sid}/content", headers=self.h, timeout=60).json()

    def shot(self, sid, full=False):
        r = httpx.get(f"{self.base}/session/{sid}/screenshot",
                      headers=self.h, params={"fullPage": full}, timeout=90)
        return r

    def pdf(self, sid):
        return httpx.get(f"{self.base}/session/{sid}/pdf", headers=self.h, timeout=90)

    def close(self, sid):
        return httpx.post(f"{self.base}/session/{sid}/close", headers=self.h, timeout=30).json()

    def list_sessions(self):
        return httpx.get(f"{self.base}/session/list", headers=self.h, timeout=15).json()


PAGE_HTML = """<!doctype html><html><head><title>Session Test</title></head><body>
<h1 id="title">original</h1>
<input id="q" value=""/>
<button id="go" onclick="document.getElementById('title').textContent='clicked-'+document.getElementById('q').value">Go</button>
<div id="out">0</div>
<script>window.counter=0;window.bump=function(){window.counter++;document.getElementById('out').textContent=window.counter;return window.counter;};</script>
</body></html>"""


def write_page(sbx, path="/home/user/workspace/page.html"):
    """写入测试页并在沙箱内起 http.server（cookies 需要 http origin）。"""
    from e2b_code_interpreter import Sandbox as CodeSandbox
    cs = CodeSandbox.connect(sbx["sandboxID"], api_key="<e2b-key-redacted>",
                             api_url=API, domain=DOMAIN)
    cs.files.write(path, PAGE_HTML)
    cs.commands.run("python3 -m http.server 8000 --directory /home/user/workspace",
                    background=True)
    time.sleep(2)
    return "http://127.0.0.1:8000/page.html"


def main():
    flavour = sys.argv[1] if len(sys.argv) > 1 else "browser"
    conc = int(sys.argv[2]) if len(sys.argv) > 2 else 3

    tpl = pick_template(flavour)
    print(f"template: {tpl['name']} ({tpl['templateCode']}) cpu={tpl['cpuCount']} mem={tpl['memoryMB']}")

    sbx = create(tpl["templateCode"])
    sid = sbx["sandboxID"]
    print(f"sandbox: {sid}")
    try:
        h = wait_health(sid)
        print("health:", json.dumps(h))
        check("browser /health ready", bool(h.get("ok")), str(h))

        b = Browser(sid, sbx["envdAccessToken"])
        bh = b.health()
        print("browser svc:", json.dumps(bh))
        check("browser svc cdpReady", bool(bh.get("cdpReady")), str(bh))

        # --- write a test page into the sandbox (served over http for cookies) ---
        url = write_page(sbx)
        print("page url:", url)

        # --- 1. session create ---
        s1 = b.new_session()
        check("session/create", bool(s1), s1)

        # --- 2. goto + wait + title ---
        r = b.act(s1, [{"type": "goto", "url": url, "waitUntil": "load", "timeoutMs": 30000},
                       {"type": "title"}])
        ok_goto = r["ok"] and r["results"][0]["ok"]
        title = r["results"][1].get("result", {}).get("title", "") if len(r["results"]) > 1 else ""
        check("act goto", ok_goto, str(r["results"][0])[:120])
        check("act title", title == "Session Test", title)

        # --- 3. fill + click + evaluate (stateful interaction) ---
        r = b.act(s1, [{"type": "fill", "selector": "#q", "value": "紫藤哥"},
                       {"type": "click", "selector": "#go"},
                       {"type": "wait", "ms": 300},
                       {"type": "evaluate", "expression": "document.getElementById('title').textContent"}])
        val = r["results"][-1].get("result", {}).get("value") if r["ok"] else None
        check("act fill", r["results"][0]["ok"], str(r["results"][0])[:120])
        check("act click", r["results"][1]["ok"], str(r["results"][1])[:120])
        check("act evaluate reads DOM", val == "clicked-紫藤哥", repr(val))

        # --- 4. JS state persists across separate HTTP calls (long session) ---
        r = b.act(s1, [{"type": "evaluate", "expression": "window.bump()"}])
        c1 = r["results"][0]["result"]["value"] if r["ok"] else None
        r2 = b.act(s1, [{"type": "evaluate", "expression": "window.counter"}])
        c2 = r2["results"][0]["result"]["value"] if r2["ok"] else None
        check("session keeps JS state across calls", c1 == 1 and c2 == 1, f"{c1} -> {c2}")

        # --- 5. screenshot / html / text ---
        r = b.act(s1, [{"type": "screenshot"}])
        png_b64 = r["results"][0].get("result", {}).get("data", "") if r["ok"] else ""
        check("act screenshot", len(png_b64) > 500, f"b64len={len(png_b64)}")
        shot = b.shot(s1)
        check("GET /session/{sid}/screenshot -> png",
              shot.status_code == 200 and shot.content[:8] == b"\x89PNG\r\n\x1a\n",
              f"{shot.status_code} {len(shot.content)}B")

        c = b.content(s1)
        check("GET /session/{sid}/content", "Session Test" in c.get("title", ""), c.get("title"))

        # --- 6. cookies ---
        r = b.act(s1, [{"type": "setCookie", "name": "agent", "value": "ziteng"},
                       {"type": "cookies"}])
        cookies = r["results"][-1].get("result", {}).get("cookies", []) if r["ok"] else []
        check("act cookies", any(ck.get("name") == "agent" for ck in cookies),
              str([ck.get("name") for ck in cookies])[:80])

        # --- 7. console logs ---
        r = b.act(s1, [{"type": "evaluate", "expression": "console.log('hi-from-agent'); 1"}])
        r = b.act(s1, [{"type": "console"}])
        logs = r["results"][0].get("result", {}).get("logs", []) if r["ok"] else []
        check("act console capture", any("hi-from-agent" in l.get("text", "") for l in logs),
              str(logs[-1:])[:120])

        # --- 8. pdf ---
        p = b.pdf(s1)
        check("GET /session/{sid}/pdf -> pdf", p.status_code == 200 and p.content[:4] == b"%PDF",
              f"{p.status_code} {len(p.content)}B")

        # --- 9. multi-session inside one sandbox (concurrent tabs) ---
        s2 = b.new_session(url)
        s3 = b.new_session(url)
        lst = b.list_sessions()
        check("multi-session within one sandbox", lst.get("count", 0) >= 3, str(lst.get("count")))

        def tab_task(s, marker):
            rr = b.act(s, [{"type": "fill", "selector": "#q", "value": marker},
                           {"type": "click", "selector": "#go"},
                           {"type": "evaluate", "expression": "document.getElementById('title').textContent"}])
            val = rr["results"][-1].get("result", {}).get("value")
            if val is None:
                print(f"    [dbg] tab {marker} act failed: {json.dumps(rr)[:400]}")
            return val

        with cf.ThreadPoolExecutor(max_workers=3) as ex:
            futs = [ex.submit(tab_task, s, m) for s, m in ((s1, "tab1"), (s2, "tab2"), (s3, "tab3"))]
            vals = [f.result(timeout=120) for f in futs]
        check("concurrent tabs isolated",
              vals == ["clicked-tab1", "clicked-tab2", "clicked-tab3"], str(vals))

        # --- 10. close ---
        check("session close", b.close(s2).get("closed") is True)
        b.close(s1)
        b.close(s3)
        lst = b.list_sessions()
        check("session list after close", lst.get("count", -1) == 0, str(lst.get("count")))
    finally:
        kill(sid)

    # --- 11. multi-sandbox concurrency ---
    print(f"\n=== concurrency: {conc} sandboxes in parallel ===")
    t0 = time.time()
    results = []

    def one(i):
        try:
            s = create(tpl["templateCode"], timeout=600)
            bb = Browser(s["sandboxID"], s["envdAccessToken"])
            hh = wait_health(s["sandboxID"], timeout=240)
            if not hh.get("ok"):
                return {"i": i, "ok": False, "err": f"health {hh}"}
            sid2 = bb.new_session("about:blank")
            rr = bb.act(sid2, [{"type": "goto", "url": "data:text/html,<h1>ok</h1>"},
                               {"type": "evaluate", "expression": "document.querySelector('h1').textContent"},
                               {"type": "screenshot"}])
            v = rr["results"][1]["result"]["value"] if rr["ok"] else None
            bb.close(sid2)
            kill(s["sandboxID"])
            return {"i": i, "ok": v == "ok", "val": v}
        except Exception as e:
            return {"i": i, "ok": False, "err": repr(e)[:200]}

    with cf.ThreadPoolExecutor(max_workers=conc) as ex:
        futs = [ex.submit(one, i) for i in range(conc)]
        for f in cf.as_completed(futs):
            results.append(f.result(timeout=420))
    elapsed = time.time() - t0
    bad = [r for r in results if not r.get("ok")]
    for r in sorted(results, key=lambda x: x["i"]):
        print("  ", r)
    check(f"concurrency n={conc} all succeed", not bad, f"{len(results) - len(bad)}/{len(results)}")
    print(f"  wall clock: {elapsed:.1f}s (avg {elapsed / max(1, conc):.1f}s/sandbox)")

    print()
    if FAILS:
        print(f"❌ {len(FAILS)} FAILED: {FAILS}")
        sys.exit(1)
    print("🎉 ALL SESSION SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
