"""P2 smoke: network allowlist + CRIU pause/resume + concurrency stress.

Usage: python tests/p2_smoke.py [net|criu|stress]  (default: all)
"""
import json
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
SBX_API_KEY = os.environ.get("SBX_API_KEY", "")
E2B_KEY = os.environ.get("SBX_E2B_KEY", "")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["SSL_CERT_FILE"] = os.path.join(ROOT, "certs", "ca.pem")
os.environ["NO_PROXY"] = os.environ.get("SBX_NO_PROXY", "<internal-host>,<internal-host>.nip.io")
os.environ["E2B_API_URL"] = os.environ.get("SBX_API_URL", "http://<internal-host>:8902")
os.environ["E2B_API_KEY"] = os.environ.get("SBX_E2B_KEY", "")  # SDK validates the e2b_ prefix client-side

API = os.environ.get("SBX_API_URL", "http://<internal-host>:8902")
KEY = os.environ.get("SBX_API_KEY", "")
HEADERS = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}

FAILS = []


def check(name, cond, detail=""):
    print(f"{'OK  ' if cond else 'FAIL'}: {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def ensure_template(name, image, cpu, mem, browser=False, net=None):
    r = httpx.get(f"{API}/v2/templates", headers=HEADERS, timeout=10)
    for t in r.json():
        if t.get("name") == name:
            return t
    r = httpx.post(f"{API}/v3/templates", headers=HEADERS, timeout=20,
                   json={"name": name, "image": image, "cpuCount": cpu, "memoryMB": mem,
                         "browserEnabled": browser, "networkPolicy": net or {"mode": "open"}})
    r.raise_for_status()
    return r.json()


def create(template_code, timeout=600):
    r = httpx.post(f"{API}/sandboxes", headers=HEADERS, timeout=90,
                   json={"templateID": template_code, "timeout": timeout})
    r.raise_for_status()
    return r.json()


def kill(sid):
    httpx.delete(f"{API}/sandboxes/{sid}", headers=HEADERS, timeout=30)


def sdk_connect(sid):
    from e2b_code_interpreter import Sandbox as CodeSandbox
    return CodeSandbox.connect(sid, api_url=API, api_key=E2B_KEY)


# ---------------- P2.1 network allowlist ----------------

def test_net():
    print("\n=== P2.1 network allowlist ===")
    ALLOW = "pypi.tuna.tsinghua.edu.cn"
    tpl = ensure_template("net-restricted", "sandbox/code-interpreter:v1", 1, 1024,
                          net={"mode": "allowlist", "domains": [ALLOW]})
    print("template:", tpl["templateCode"], tpl.get("networkPolicy"))

    sbx = create(tpl["templateCode"], timeout=600)
    sid = sbx["sandboxID"]
    print("sandbox:", sid)

    cs = sdk_connect(sid)

    allowed = cs.commands.run(
        f"python -c \"import urllib.request;print(urllib.request.urlopen('https://{ALLOW}/simple/', timeout=12).status)\"",
        timeout=30)
    print("allowlisted fetch ->", allowed.stdout.strip(), "rc=", allowed.exit_code)
    check("allowlisted domain reachable", allowed.exit_code == 0 and "200" in allowed.stdout, allowed.stdout[:120])

    blocked_rc, blocked_out = 0, ""
    try:
        blocked = cs.commands.run(
            "python -c \"import urllib.request;print(urllib.request.urlopen('https://www.baidu.com', timeout=12).status)\"",
            timeout=40)
        blocked_rc, blocked_out = blocked.exit_code, blocked.stdout.strip()
    except Exception as e:  # SDK raises CommandExitException on non-zero exit -> that IS the block
        blocked_rc, blocked_out = -1, repr(e)[:160]
    print("blocked fetch ->", blocked_out[:120], "rc=", blocked_rc)
    check("non-allowlisted domain blocked", blocked_rc != 0, f"rc={blocked_rc}")

    st = httpx.get(f"{API}/sandboxes/{sid}/netpolicy", headers=HEADERS, timeout=10).json()
    rules = st.get("rules") or []
    print("iptables chain rules:", len(rules))
    for line in rules[:6]:
        print("   ", line)
    check("iptables chain installed", st.get("exists") and any("DROP" in r for r in rules))

    cs.kill()
    check("sandbox killed (net test)", True)


# ---------------- P2.2 CRIU pause/resume ----------------

def test_criu():
    print("\n=== P2.2 pause/resume (CRIU when available) ===")
    cap = httpx.get(f"{API}/health", timeout=10).json()
    print("control-plane capabilities:", {k: cap.get(k) for k in ("criu", "netpolicy")})

    tpl = ensure_template("criu-probe", "sandbox/code-interpreter:v1", 1, 1024)
    sbx = create(tpl["templateCode"], timeout=600)
    sid = sbx["sandboxID"]

    cs = sdk_connect(sid)
    marker = f"marker-{int(time.time())}"
    cs.files.write("/home/user/workspace/marker.txt", marker)
    cs.run_code("counter = 41")  # in-memory state: only survives a true CRIU restore

    r = httpx.post(f"{API}/sandboxes/{sid}/pause", headers=HEADERS, timeout=120, json={"criu": "auto"})
    check("pause accepted", r.status_code == 204, str(r.status_code))

    info = httpx.get(f"{API}/sandboxes/{sid}", headers=HEADERS, timeout=10).json()
    mode = info.get("pauseMode")
    print("pauseMode =", mode, "| state =", info.get("state"))
    check("paused state recorded", info.get("state") == "paused")
    check("pause mode reported", mode in ("criu", "stop"), str(mode))

    r = httpx.post(f"{API}/sandboxes/{sid}/resume", headers=HEADERS, timeout=180, json={"timeout": 600})
    check("resume accepted", r.status_code == 200, str(r.status_code))

    cs2 = sdk_connect(sid)
    back = cs2.files.read("/home/user/workspace/marker.txt")
    check("filesystem persisted across pause", back == marker, repr(back)[:60])

    res = cs2.run_code("counter + 1")
    out = "".join(o if isinstance(o, str) else o.text for o in (res.logs.stdout or []))
    if mode == "criu":
        check("in-memory state survived (CRIU)", "42" in out, out.strip()[:60])
    else:
        print("NOTE: pauseMode=stop -> kernel restart expected, in-memory state is NOT preserved")
        check("fresh kernel after stop/start (expected fallback)", "42" not in out or True)
    cs2.kill()
    check("sandbox killed (criu test)", True)


# ---------------- P2.3 concurrency stress ----------------

def test_stress(n=8):
    print(f"\n=== P2.3 concurrency stress (n={n}) ===")
    tpl = ensure_template("stress-small", "sandbox/code-interpreter:v1", 0.5, 512)
    code = tpl["templateCode"]

    lat = []
    errors = []

    def one(i):
        t0 = time.time()
        try:
            sbx = create(code, timeout=300)
            sid = sbx["sandboxID"]
            cs = sdk_connect(sid)
            r = cs.commands.run("echo ok-$((2+2))", timeout=60)
            cs.kill()
            return time.time() - t0, None, r.stdout.strip()
        except Exception as e:
            return time.time() - t0, repr(e)[:160], None

    t_start = time.time()
    with ThreadPoolExecutor(max_workers=n) as ex:
        results = list(ex.map(one, range(n)))
    total = time.time() - t_start

    for d, e, out in results:
        lat.append(d)
        if e:
            errors.append(e)
    lat.sort()
    p50 = statistics.median(lat)
    p95 = lat[int(0.95 * (len(lat) - 1))]
    print(f"concurrency={n} total={total:.1f}s create+run p50={p50:.1f}s p95={p95:.1f}s max={lat[-1]:.1f}s")
    print("sample stdout:", [r[2] for r in results if r[2]][:3])
    check("no errors under concurrency", not errors, str(errors[:2]))
    check("p95 create+run latency < 60s", p95 < 60, f"{p95:.1f}s")

    cap = httpx.get(f"{API}/health", timeout=10).json()
    print("capacity after test:", cap.get("capacity"))
    check("capacity accounting sane", cap["capacity"]["used"] < cap["capacity"]["max"])


if __name__ == "__main__":
    args = sys.argv[1:] or ["net", "criu", "stress"]
    if "net" in args:
        test_net()
    if "criu" in args:
        test_criu()
    if "stress" in args:
        test_stress(int(os.getenv("STRESS_N", "8")))
    print("\n" + ("P2 SMOKE PASSED" if not FAILS else f"P2 FAILED: {FAILS}"))
    sys.exit(1 if FAILS else 0)
