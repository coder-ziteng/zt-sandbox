"""End-to-end smoke test for P3 第五刀 Diagnostic API.

Coverage:
  1. Default GET /sandboxes/{id}/diag returns all 5 sections, each with data
  2. ?include= subset returns only requested sections
  3. Stats sanity: cpuPct/mem/net fields are present and numeric
  4. Processes section shows real processes (>= 1 process, contains "python"
     or similar since envd is a Python service)
  5. Connections section shows the envd listening port
  6. envd section: reachable=true, statusCode=200, latencyMs > 0
  7. Logs section: stdout/stderr keys present (may be empty for fresh container)
  8. Container meta block has id/name/image/status/ipAddress
  9. Unknown include returns error with valid list
 10. Non-existent sandbox → 404

Run from project root:
    python -m tests.diag_smoke
"""
from __future__ import annotations

import os
import time

import httpx

API_URL = os.environ.get("SBX_API_URL", "http://127.0.0.1:8902")
API_KEY = os.environ["SBX_API_KEY"]
HEADERS = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}

ALL_SECTIONS = ("processes", "stats", "logs", "connections", "envd")


def must(cond, msg):
    if not cond:
        raise SystemExit(f"✗ FAIL: {msg}")
    print(f"  ✓ {msg}")


def http_call(method, path, body=None, expect=None, params=None):
    r = httpx.request(method, f"{API_URL}{path}", headers=HEADERS, json=body,
                      params=params, timeout=60)
    if expect is not None and r.status_code != expect:
        raise SystemExit(f"✗ {method} {path} expected {expect}, got {r.status_code}: {r.text[:200]}")
    return r


def pick_template() -> str:
    rows = http_call("GET", "/v2/templates", expect=200).json()
    for r in rows:
        if "interpreter" in r.get("name", "").lower():
            return r["templateCode"]
    return rows[0]["templateCode"]


def create_sandbox(template_code: str) -> str:
    return http_call("POST", "/sandboxes", body={"templateID": template_code, "timeout": 600},
                     expect=201).json()["sandboxID"]


def kill_sandbox(sid: str):
    httpx.delete(f"{API_URL}/sandboxes/{sid}", headers=HEADERS, timeout=30)


def get_diag(sid, params=None):
    return http_call("GET", f"/sandboxes/{sid}/diag", params=params, expect=200).json()


def main():
    template = pick_template()
    print(f"using template: {template}")
    sid = create_sandbox(template)
    print(f"  → sandbox {sid}")
    try:
        # ---- 1. Default GET returns all 5 sections + container meta ----
        print("\n[1] default GET returns all 5 sections + container meta")
        d = get_diag(sid)
        must(d["sandboxID"] == sid, f"sandboxID matches ({sid})")
        must("container" in d and d["container"].get("status") == "running",
             f"container meta status=running (got {d.get('container')})")
        must(d["container"].get("name") == f"sbx-{sid}",
             f"container name = sbx-{sid}")
        # host-network 沙箱 ipAddress 为 127.0.0.1; bridge 沙箱为 bridge IP
        ip = d["container"].get("ipAddress", "")
        mode = d["container"].get("networkMode", "")
        must(ip,
             f"container has ipAddress (got {ip!r}, networkMode={mode!r})")
        must(mode in ("host", "bridge"),
             f"container networkMode in host|bridge (got {mode!r})")
        if mode == "host":
            must(ip == "127.0.0.1",
                 f"host-network → ipAddress=127.0.0.1 (got {ip!r})")
        for s in ALL_SECTIONS:
            must(s in d, f"section {s} present")
            must("error" not in d[s], f"section {s} has no error (got {d[s]})")

        # ---- 2. Stats sanity ----
        print("\n[2] stats fields present and numeric")
        st = d["stats"]
        for k in ("memUsageBytes", "memLimitBytes", "netRxBytes", "netTxBytes",
                  "blockReadBytes", "blockWriteBytes"):
            must(isinstance(st.get(k), int), f"stats.{k} is int ({st.get(k)})")
        must(st["memLimitBytes"] > 0, f"memLimitBytes > 0 (got {st['memLimitBytes']})")
        must(isinstance(st.get("cpuPct"), (int, float)) or st.get("cpuPct") is None,
             f"stats.cpuPct is numeric or null (got {st.get('cpuPct')})")

        # ---- 3. Processes sanity ----
        print("\n[3] processes section shows real running processes")
        procs = d["processes"]
        must(procs["count"] >= 1, f"at least 1 process (got {procs['count']})")
        cmds = " ".join(str(p.get("cmd", "")) for p in procs["list"])
        must("python" in cmds.lower() or "/init" in cmds or "sh" in cmds,
             f"processes include python/init/sh (sampled cmds: {cmds[:120]})")

        # ---- 4. Connections section shows envd listening ----
        print("\n[4] connections section shows listening ports")
        conns = d["connections"]
        must("error" not in conns,
             f"connections has no error (got {conns})")
        must(conns["count"] >= 1, f"at least 1 listening socket (got {conns['count']})")
        locals_ = [c["local"] for c in conns["list"]]
        # envd 在容器内监听; 端口可能是 20000 (host-network 分配) 或 49983 (镜像默认)
        must(any(":20000" in l or ":49983" in l for l in locals_),
             f"envd port (20000 or 49983) listening (locals: {locals_[:6]})")

        # ---- 5. envd probe ----
        print("\n[5] envd probe reachable + latency")
        env = d["envd"]
        must(env.get("reachable") is True, f"envd reachable (got {env})")
        must(env.get("statusCode") == 200, f"envd statusCode=200 (got {env.get('statusCode')})")
        must(isinstance(env.get("latencyMs"), (int, float)) and env["latencyMs"] >= 0,
             f"envd latencyMs present (got {env.get('latencyMs')})")
        must(env.get("raw", {}).get("ok") is True,
             f"envd raw.ok=True (got {env.get('raw')})")

        # ---- 6. Logs section shape ----
        print("\n[6] logs section shape (stdout/stderr strings)")
        lg = d["logs"]
        must("stdout" in lg and isinstance(lg["stdout"], str), f"logs.stdout str (got {type(lg.get('stdout'))})")
        must("stderr" in lg and isinstance(lg["stderr"], str), f"logs.stderr str")
        must(lg.get("linesShown") == 100, f"logs.linesShown=100 default (got {lg.get('linesShown')})")
        # custom logTail
        lg50 = get_diag(sid, params={"include": "logs", "logTail": "50"})["logs"]
        must(lg50.get("linesShown") == 50, f"logs.linesShown=50 with ?logTail=50 (got {lg50.get('linesShown')})")

        # ---- 7. ?include= subset ----
        print("\n[7] ?include= subset returns only requested sections")
        sub = get_diag(sid, params={"include": "processes,envd"})
        must(set(sub.keys()) >= {"sandboxID", "container", "processes", "envd"},
             f"subset has only processes+envd (keys: {sorted(sub.keys())})")
        must("stats" not in sub and "logs" not in sub and "connections" not in sub,
             f"stats/logs/connections absent in subset")
        must(sub["processes"]["count"] >= 1, "subset processes still populated")
        must(sub["envd"]["reachable"] is True, "subset envd still populated")

        # ---- 8. Unknown include returns error ----
        print("\n[8] unknown include section returns error with valid list")
        bad = get_diag(sid, params={"include": "processes,bogus"})
        must("error" in bad and "bogus" in bad["error"],
             f"unknown include error (got {bad})")
        must(set(bad.get("valid", [])) == set(ALL_SECTIONS),
             f"valid list = all 5 sections (got {bad.get('valid')})")

        # ---- 9. Non-existent sandbox → 404 ----
        print("\n[9] non-existent sandbox returns 404")
        r = httpx.get(f"{API_URL}/sandboxes/sbxdoesnotexist/diag",
                      headers=HEADERS, timeout=15)
        must(r.status_code == 404, f"non-existent sandbox → 404 (got {r.status_code})")

        # ---- 10. Stats refresh: call twice, mem/network counters monotonic-ish ----
        print("\n[10] stats: counters refresh between two calls")
        s1 = get_diag(sid, params={"include": "stats"})["stats"]
        time.sleep(1.2)
        s2 = get_diag(sid, params={"include": "stats"})["stats"]
        # rx/tx are cumulative; second call should be >= first (allowing equality on idle)
        must(s2["netRxBytes"] >= s1["netRxBytes"],
             f"netRxBytes monotonic ({s1['netRxBytes']} → {s2['netRxBytes']})")
        must(s2["netTxBytes"] >= s1["netTxBytes"],
             f"netTxBytes monotonic ({s1['netTxBytes']} → {s2['netTxBytes']})")

    finally:
        kill_sandbox(sid)

    print("\nALL DIAGNOSTIC SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
