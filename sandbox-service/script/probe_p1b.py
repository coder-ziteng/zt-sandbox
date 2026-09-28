"""P1+ 问题探测：带 url 建 session 后页面到底加载没有 + setCookie/cookies 细节。"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["SSL_CERT_FILE"] = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "certs", "ca.pem")
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "*"

import httpx

API = "http://<internal-host>:8902"
DOMAIN = "<internal-host>.nip.io"
HEADERS = {"Authorization": "Bearer <dev-key-redacted>", "Content-Type": "application/json"}

PAGE = """<!doctype html><html><head><title>Probe</title></head><body>
<h1 id="title">probe</h1><input id="q" value=""/>
</body></html>"""


def main():
    r = httpx.get(f"{API}/v2/templates", headers=HEADERS, timeout=10)
    tpl = [t for t in r.json() if t.get("name") == "browser"][0]
    r = httpx.post(f"{API}/sandboxes", headers=HEADERS,
                   json={"templateID": tpl["templateCode"], "timeout": 600}, timeout=180)
    sbx = r.json()
    sid = sbx["sandboxID"]
    token = sbx["envdAccessToken"]
    print("sandbox:", sid)
    base = f"https://3000-{sid}.{DOMAIN}"
    H = {"X-Access-Token": token, "Content-Type": "application/json"}

    try:
        # 起 http server
        from e2b_code_interpreter import Sandbox as CodeSandbox
        cs = CodeSandbox.connect(sid, api_key="<e2b-key-redacted>", api_url=API, domain=DOMAIN)
        cs.files.write("/home/user/workspace/page.html", PAGE)
        cs.commands.run("python3 -m http.server 8000 --directory /home/user/workspace", background=True)
        time.sleep(2)

        url = "http://127.0.0.1:8000/page.html"
        # 等 browser 就绪
        for _ in range(60):
            try:
                bh = httpx.get(f"{base}/health", headers=H, timeout=5).json()
                if bh.get("cdpReady"):
                    break
            except Exception:
                pass
            time.sleep(2)

        # 1) 带 url 建 session，看页面加载状态
        r = httpx.post(f"{base}/session/create", headers=H, json={"url": url}, timeout=60)
        s1 = r.json()["sessionId"]
        time.sleep(2)
        r = httpx.post(f"{base}/session/{s1}/act", headers=H, json={"actions": [
            {"type": "evaluate", "expression": "location.href"},
            {"type": "evaluate", "expression": "document.title"},
            {"type": "evaluate", "expression": "document.readyState"},
            {"type": "evaluate", "expression": "!!document.querySelector('#q')"},
        ]}, timeout=60)
        print("s1 (url at create):", json.dumps(r.json()["results"], ensure_ascii=False))

        # 2) 第二个带 url 的 session
        r = httpx.post(f"{base}/session/create", headers=H, json={"url": url}, timeout=60)
        s2 = r.json()["sessionId"]
        time.sleep(2)
        r = httpx.post(f"{base}/session/{s2}/act", headers=H, json={"actions": [
            {"type": "evaluate", "expression": "location.href"},
            {"type": "evaluate", "expression": "document.title"},
            {"type": "evaluate", "expression": "!!document.querySelector('#q')"},
        ]}, timeout=60)
        print("s2 (url at create):", json.dumps(r.json()["results"], ensure_ascii=False))

        # 3) 显式 goto 的 session
        r = httpx.post(f"{base}/session/create", headers=H, json={"url": "about:blank"}, timeout=60)
        s3 = r.json()["sessionId"]
        r = httpx.post(f"{base}/session/{s3}/act", headers=H, json={"actions": [
            {"type": "goto", "url": url, "waitUntil": "load", "timeoutMs": 15000},
            {"type": "evaluate", "expression": "document.title"},
            {"type": "evaluate", "expression": "!!document.querySelector('#q')"},
        ]}, timeout=90)
        print("s3 (explicit goto):", json.dumps(r.json()["results"], ensure_ascii=False))

        # 4) cookies 细节
        r = httpx.post(f"{base}/session/{s3}/act", headers=H, json={"actions": [
            {"type": "setCookie", "name": "probe", "value": "v1"},
            {"type": "cookies"},
            {"type": "evaluate", "expression": "document.cookie"},
        ]}, timeout=60)
        print("s3 cookies:", json.dumps(r.json()["results"], ensure_ascii=False))

        for s in (s1, s2, s3):
            httpx.post(f"{base}/session/{s}/close", headers=H, timeout=15)
    finally:
        httpx.delete(f"{API}/sandboxes/{sid}", headers=HEADERS, timeout=30)
        print("killed")


main()
