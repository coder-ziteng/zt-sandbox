"""End-to-end smoke test for P3 Lifecycle Hook + watchdog.

Exercises:
  1. PUT /templates/{code}/hooks  — install/uninstall hooks on an existing template
  2. Startup hook (success) — sandbox created, hook recorded in hook_state
  3. Startup hook (fail-closed fail) — creation returns 500, sandbox torn down
  4. Startup hook (non-blocking fail) — sandbox still created, hook error recorded
  5. Periodic hook — verify it fires within one tick (~30s) and result is recorded

Run from project root:
    python -m tests.hook_smoke

Required env:
  SBX_API_URL   control plane base URL (default http://127.0.0.1:8902)
  SBX_API_KEY   control plane API key (Bearer)
  SBX_TEMPLATE  base template code (defaults to first one in /v2/templates)
"""
from __future__ import annotations

import os
import time

import httpx

API_URL = os.environ.get("SBX_API_URL", "http://127.0.0.1:8902")
API_KEY = os.environ["SBX_API_KEY"]
HEADERS = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
TEMPLATE_BASE = os.environ.get("SBX_TEMPLATE")

PERIODIC_TICK_S = 30  # must match control plane HOOK_TICK_S


def must(cond, msg):
    if not cond:
        raise SystemExit(f"✗ FAIL: {msg}")
    print(f"  ✓ {msg}")


def http_call(method, path, body=None, expect=None):
    r = httpx.request(method, f"{API_URL}{path}", headers=HEADERS, json=body, timeout=60)
    if expect is not None:
        must(r.status_code == expect,
             f"{method} {path} expected {expect}, got {r.status_code} :: {r.text[:200]}")
    return r


def pick_template() -> str:
    if TEMPLATE_BASE:
        return TEMPLATE_BASE
    rows = http_call("GET", "/v2/templates", expect=200).json()
    if not rows:
        raise SystemExit("no templates found; create one first")
    return rows[0]["templateCode"]


def create_template(name, image, startup_hooks=None, periodic_hooks=None) -> str:
    body = {"name": name, "image": image, "cpuCount": 1, "memoryMB": 1024, "diskSizeMB": 1024,
            "startupHooks": startup_hooks or [], "periodicHooks": periodic_hooks or []}
    r = http_call("POST", "/v3/templates", body=body, expect=201)
    return r.json()["templateCode"]


def reset_hooks(template_code, startup=None, periodic=None):
    """Restore base template hooks to empty (smoke isolation)."""
    body = {"startupHooks": startup or [], "periodicHooks": periodic or []}
    http_call("PUT", f"/templates/{template_code}/hooks", body=body, expect=200)


def create_sandbox(template_code, timeout=300):
    r = http_call("POST", "/sandboxes", body={"templateID": template_code, "timeout": timeout},
                  expect=201)
    return r.json()["sandboxID"]


def kill_sandbox(sid):
    httpx.delete(f"{API_URL}/sandboxes/{sid}", headers=HEADERS, timeout=30)


def main():
    base_tpl = pick_template()
    print(f"using base template: {base_tpl}")

    # ---- 1. PUT /templates/{code}/hooks round-trip ----
    print("\n[1] PUT hooks round-trip")
    hooks_in = {
        "startupHooks": [{"name": "noop", "command": "echo hi"}],
        "periodicHooks": [{"name": "noop-periodic", "command": "echo p", "interval_s": 60}],
    }
    r = http_call("PUT", f"/templates/{base_tpl}/hooks", body=hooks_in, expect=200).json()
    must(r["startupHooks"][0]["name"] == "noop", "startup hook persisted")
    must(r["periodicHooks"][0]["interval_s"] == 60, "periodic hook interval persisted")
    reset_hooks(base_tpl)  # restore base state

    # ---- 2. Startup hook (success path) ----
    print("\n[2] startup hook success path")
    succ_tpl = create_template("hook-smoke-success",
                                "sandbox/code-interpreter:v1",
                                startup_hooks=[
                                    {"name": "marker",
                                     "command": "mkdir -p /tmp/hook && touch /tmp/hook/ok && echo MARKER_OK",
                                     "timeout_s": 30}])
    try:
        sid = create_sandbox(succ_tpl)
        print(f"  → sandbox {sid}")
        hs = http_call("GET", f"/sandboxes/{sid}/hooks/status", expect=200).json()
        sresults = hs["hookState"]["startup"]["results"]
        must(sresults[0]["ok"], "startup hook ok=True")
        must(sresults[0]["exit_code"] == 0, "startup hook exit_code=0")
        must("MARKER_OK" in sresults[0]["stdout"], "stdout captured")
        kill_sandbox(sid)
    finally:
        httpx.delete(f"{API_URL}/templates/{succ_tpl}", headers=HEADERS, timeout=15)

    # ---- 3. Startup hook (fail-closed) ----
    print("\n[3] startup hook fail-closed (blocking)")
    fail_tpl = create_template("hook-smoke-fail-closed",
                                "sandbox/code-interpreter:v1",
                                startup_hooks=[
                                    {"name": "must-pass",
                                     "command": "exit 7",
                                     "fail_closed": True,
                                     "timeout_s": 10}])
    try:
        r = httpx.post(f"{API_URL}/sandboxes", headers=HEADERS,
                       json={"templateID": fail_tpl, "timeout": 120}, timeout=60)
        must(r.status_code == 500, f"create returns 500 (got {r.status_code})")
        body = r.json()
        must("启动钩子失败" in body["message"] or "must-pass" in body["message"],
             f"error mentions hook failure: {body['message']!r}")
        # No sandbox should remain on the server (template torn down on fail-closed).
        listing = http_call("GET", "/v2/sandboxes?state=running", expect=200).json()
        must(not any("hook-smoke-fail-closed" == s.get("templateID") for s in listing),
             "no orphan sandbox for fail-closed template")
    finally:
        httpx.delete(f"{API_URL}/templates/{fail_tpl}", headers=HEADERS, timeout=15)

    # ---- 4. Startup hook (non-blocking fail) ----
    print("\n[4] startup hook non-blocking fail (fail_closed=False)")
    nb_tpl = create_template("hook-smoke-nb",
                              "sandbox/code-interpreter:v1",
                              startup_hooks=[
                                  {"name": "soft-fail",
                                   "command": "exit 1",
                                   "fail_closed": False,
                                   "timeout_s": 10},
                                  {"name": "marker2",
                                   "command": "echo NONBLOCK_OK",
                                   "timeout_s": 10}])
    try:
        sid = create_sandbox(nb_tpl)
        hs = http_call("GET", f"/sandboxes/{sid}/hooks/status", expect=200).json()
        sresults = hs["hookState"]["startup"]["results"]
        must(not sresults[0]["ok"] and sresults[0]["name"] == "soft-fail",
             "first hook recorded as failed")
        must(sresults[1]["ok"], "second hook ran anyway")
        # non-blocking failure does NOT fail the overall all_passed gate
        must(hs["hookState"]["startup"]["all_passed"] is True,
             "all_passed=True (non-blocking failure tolerated)")
        must(hs["hookState"]["startup"]["blocking_failures"] == [],
             "blocking_failures empty")
        kill_sandbox(sid)
    finally:
        httpx.delete(f"{API_URL}/templates/{nb_tpl}", headers=HEADERS, timeout=15)

    # ---- 5. Periodic hook ----
    print("\n[5] periodic hook fires within ~one tick")
    per_tpl = create_template("hook-smoke-periodic",
                               "sandbox/code-interpreter:v1",
                               periodic_hooks=[
                                   {"name": "tick-marker",
                                    "command": "echo TICK",
                                    "interval_s": 20}])
    try:
        sid = create_sandbox(per_tpl, timeout=180)
        # first periodic tick can be up to HOOK_TICK_S (30s default) after create
        deadline = time.time() + PERIODIC_TICK_S + 45
        ok = False
        while time.time() < deadline:
            hs = http_call("GET", f"/sandboxes/{sid}/hooks/status", expect=200).json()
            pstate = (hs.get("hookState") or {}).get("periodic", {}).get("hooks", {})
            if pstate.get("tick-marker", {}).get("last_run", 0) > 0:
                ok = True
                break
            time.sleep(3)
        must(ok, "periodic hook ran at least once")
        # last_ok captured
        hs = http_call("GET", f"/sandboxes/{sid}/hooks/status", expect=200).json()
        marker = hs["hookState"]["periodic"]["hooks"]["tick-marker"]
        must(marker.get("last_ok") is True, f"periodic hook ok=True (got {marker})")
        kill_sandbox(sid)
    finally:
        httpx.delete(f"{API_URL}/templates/{per_tpl}", headers=HEADERS, timeout=15)

    print("\nALL HOOK SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()