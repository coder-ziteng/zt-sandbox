"""Speed benchmark: N cold lifecycle runs + N warm executions on one sandbox.

Usage: python tests/bench_speed.py <api-key> [rounds=10]
Phases measured per cold run: create / health / first_execute / kill / total.
Warm phase: one sandbox, `rounds` executions, per-execute latency only.
"""
import json
import statistics
import sys
import time

import httpx

import bubble_sort_demo as bsd

ROUNDS = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 10

WORKLOAD = '''
import random
def bubble_sort(arr):
    a = arr[:]
    n = len(a)
    for i in range(n - 1):
        swapped = False
        for j in range(n - 1 - i):
            if a[j] > a[j + 1]:
                a[j], a[j + 1] = a[j + 1], a[j]
                swapped = True
        if not swapped:
            break
    return a
data = [random.randint(0, 100000) for _ in range(500)]
result = bubble_sort(data)
assert result == sorted(data)
print("bubble-sort-500 OK")
'''


def create():
    r = httpx.post(f"{bsd.API}/sandboxes", headers=bsd.HEADERS, timeout=90,
                   json={"templateID": TID, "timeout": 600})
    r.raise_for_status()
    return r.json()


def wait_health(sid):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        h = httpx.get(f"{bsd.API}/sandboxes/{sid}/health", headers=bsd.HEADERS, timeout=10).json()
        if all(h.get(k, {}).get("ok") for k in ("envd", "jupyter")):
            return
        time.sleep(0.5)
    raise RuntimeError(f"sandbox {sid} not healthy")


def execute(sid, token):
    url = f"https://127.0.0.1:{TPORT}/execute"
    headers = {"Host": f"49999-{sid}.{bsd.DOMAIN}", "X-Access-Token": token,
               "Content-Type": "application/json"}
    out = []
    with httpx.stream("POST", url, timeout=120, verify=False, headers=headers,
                      json={"code": WORKLOAD, "context_id": "default"}) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line.strip():
                continue
            ev = json.loads(line)
            if ev.get("type") == "stdout":
                out.append(ev["text"].strip())
            elif ev.get("type") == "error":
                raise RuntimeError(f"exec error: {ev}")
    return " | ".join(out)


def kill(sid):
    httpx.delete(f"{bsd.API}/sandboxes/{sid}", headers=bsd.HEADERS, timeout=30)


def timed(fn):
    t0 = time.perf_counter()
    r = fn()
    return r, time.perf_counter() - t0


def stats(vals):
    return (f"avg={statistics.mean(vals)*1000:7.1f}ms  min={min(vals)*1000:7.1f}ms  "
            f"max={max(vals)*1000:7.1f}ms  stdev={statistics.stdev(vals)*1000:6.1f}ms")


def main():
    global TID, TPORT
    r = httpx.get(f"{bsd.API}/v2/templates", headers=bsd.HEADERS, timeout=10)
    TID = next(t["templateID"] for t in r.json() if t["name"] == "code-interpreter")
    ssh, TPORT = bsd.ssh_tunnel()

    print(f"\n=== Phase A: cold lifecycle x{ROUNDS} (create -> health -> first exec -> kill) ===")
    cold = {"create": [], "health": [], "exec": [], "kill": [], "total": []}
    try:
        for i in range(ROUNDS):
            t0 = time.perf_counter()
            info, dt = timed(create)
            cold["create"].append(dt)
            sid, token = info["sandboxID"], info.get("envdAccessToken", "")
            _, dt = timed(lambda: wait_health(sid))
            cold["health"].append(dt)
            out, dt = timed(lambda: execute(sid, token))
            cold["exec"].append(dt)
            _, dt = timed(lambda: kill(sid))
            cold["kill"].append(dt)
            cold["total"].append(time.perf_counter() - t0)
            print(f"  run {i+1:2d}: sid={sid}  exec={dt*1000:.0f}ms  out={out}")
    except Exception as e:
        print(f"  [ABORT] cold run failed: {e}")
    for k in ("create", "health", "exec", "kill", "total"):
        if cold[k]:
            print(f"  {k:6s}: {stats(cold[k])}")

    print(f"\n=== Phase B: warm exec x{ROUNDS} (single sandbox, kernel reuse) ===")
    info = create()
    sid, token = info["sandboxID"], info.get("envdAccessToken", "")
    wait_health(sid)
    warm = []
    try:
        for i in range(ROUNDS):
            _, dt = timed(lambda: execute(sid, token))
            warm.append(dt)
            print(f"  run {i+1:2d}: exec={dt*1000:.0f}ms")
    finally:
        kill(sid)
    print(f"  first (incl. kernel start): {warm[0]*1000:.1f}ms")
    print(f"  warm exec:  {stats(warm)}")
    print(f"  warm exec (exclude first): {stats(warm[1:])}")

    ssh.close()


if __name__ == "__main__":
    main()
