"""End-to-end smoke test for P4 第六刀 — chat-session 隔离 (Session = Sandbox).

Coverage:
  1. POST /sandboxes with sessionId → 201, response echoes sessionID
  2. Same sessionId again under same key → 409 (only one live sandbox per session)
  3. Different sessionId → 201 (sessions independent)
  4. GET /sandboxes/{sid} WITHOUT X-Session-Id on a bound sandbox → 403
  5. GET /sandboxes/{sid} WITH matching X-Session-Id → 200
  6. GET /sandboxes/{sid} WITH mismatched X-Session-Id → 403
  7. GET /v2/sandboxes with X-Session-Id: chat-A → list scoped to A only
  8. GET /v2/sandboxes with X-Session-Id: chat-B → list scoped to B only
  9. GET /v2/sandboxes WITHOUT X-Session-Id → all visible (admin view)
 10. edge-proxy: https://{port}-{sid}.{DOMAIN}/health WITH matching X-Session-Id → 200 (forwarded)
 11. edge-proxy: same WITHOUT X-Session-Id on a bound sandbox → 403 (rejected at edge)
 12. edge-proxy: WITH mismatched X-Session-Id → 403
 13. DELETE session-A sandbox, then re-create with same sessionId → 201
 14. Backward-compat: legacy sandbox without sessionId is reachable without
     X-Session-Id header (no regression to P4 第四刀).

Run from project root (with the envs from .env loaded):
    SBX_API_KEY=... SBX_DOMAIN=... python -m tests.session_smoke
"""
from __future__ import annotations

import os
import sys
import time
import uuid
from typing import Union

import httpx

API_URL = os.environ.get("SBX_API_URL", "http://127.0.0.1:8902")
API_KEY = os.environ.get("SBX_API_KEY", "")
DOMAIN = os.environ.get("SBX_DOMAIN") or os.environ.get("SBX_EDGE_HOST") or ""
if not API_KEY:
    sys.exit("[!] SBX_API_KEY env var is required")
if not DOMAIN:
    sys.exit("[!] SBX_DOMAIN (or SBX_EDGE_HOST) env var is required for the edge-proxy leg")

if os.environ.get("SBX_INSECURE") == "1":
    HTTPS_VERIFY: Union[bool, str] = False
else:
    ca = os.environ.get("SBX_CA_CERT")
    HTTPS_VERIFY = ca if ca and os.path.isfile(ca) else True


def must(cond, msg):
    if not cond:
        raise SystemExit(f"✗ FAIL: {msg}")
    print(f"  ✓ {msg}")


def ctrl(method, path, *, session=None, body=None, expect=None, params=None, timeout=60):
    h = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    if session:
        h["X-Session-Id"] = session
    r = httpx.request(method, f"{API_URL}{path}", headers=h, json=body,
                      params=params, timeout=timeout)
    if expect is not None and r.status_code != expect:
        raise SystemExit(
            f"✗ {method} {path} expected {expect}, got {r.status_code}: {r.text[:200]}"
        )
    return r




# ---------- discovery ----------

TPL_LIST = ctrl("GET", "/v2/templates", expect=200).json()
TPL_CODE = next((t["templateID"] for t in TPL_LIST if t.get("name") == "code-interpreter"),
                TPL_LIST[0]["templateID"])
print(f"[setup] using template {TPL_CODE}")

# Distinct session ids so reruns never collide with prior (killed) state.
SESSION_A = f"chat-A-{uuid.uuid4().hex[:8]}"
SESSION_B = f"chat-B-{uuid.uuid4().hex[:8]}"
SBX_A = SBX_B = None
TOKEN_A = TOKEN_B = None


def main():
    global SBX_A, SBX_B, TOKEN_A, TOKEN_B

    print("\n[1] create sandbox with sessionId → 201 + sessionID echoed")
    r = ctrl("POST", "/sandboxes", body={"templateID": TPL_CODE, "sessionId": SESSION_A}, expect=201)
    SBX_A = r.json()["sandboxID"]
    TOKEN_A = r.json()["envdAccessToken"]
    must(r.json().get("sessionID") == SESSION_A, f"create response carries sessionID={SESSION_A}")
    print(f"    SBX_A={SBX_A}")

    print("\n[2] duplicate sessionId → 409")
    r = ctrl("POST", "/sandboxes", body={"templateID": TPL_CODE, "sessionId": SESSION_A})
    must(r.status_code == 409, f"second create with same sessionId rejected (got {r.status_code})")

    print("\n[3] different sessionId → 201")
    r = ctrl("POST", "/sandboxes", body={"templateID": TPL_CODE, "sessionId": SESSION_B}, expect=201)
    SBX_B = r.json()["sandboxID"]
    TOKEN_B = r.json()["envdAccessToken"]
    print(f"    SBX_B={SBX_B}")

    print("\n[4] GET bound sandbox WITHOUT X-Session-Id → 403")
    ctrl("GET", f"/sandboxes/{SBX_A}", expect=403)
    must(True, "missing X-Session-Id → 403")

    print("\n[5] GET bound sandbox WITH matching X-Session-Id → 200")
    r = ctrl("GET", f"/sandboxes/{SBX_A}", session=SESSION_A, expect=200)
    must(r.json()["sandboxID"] == SBX_A, "match returns the right row")

    print("\n[6] GET bound sandbox WITH mismatched X-Session-Id → 403")
    r = ctrl("GET", f"/sandboxes/{SBX_A}", session=SESSION_B)
    must(r.status_code == 403, f"cross-session read denied (got {r.status_code})")

    print("\n[7] /v2/sandboxes scoped by X-Session-Id: chat-A")
    rows = ctrl("GET", "/v2/sandboxes", session=SESSION_A, expect=200).json()
    ids = [s["sandboxID"] for s in rows]
    must(SBX_A in ids and SBX_B not in ids,
         f"list scoped to chat-A: {[s[-8:] for s in ids]} contains A, not B")

    print("\n[8] /v2/sandboxes scoped by X-Session-Id: chat-B")
    rows = ctrl("GET", "/v2/sandboxes", session=SESSION_B, expect=200).json()
    ids = [s["sandboxID"] for s in rows]
    must(SBX_B in ids and SBX_A not in ids,
         f"list scoped to chat-B: {[s[-8:] for s in ids]} contains B, not A")

    print("\n[9] /v2/sandboxes WITHOUT X-Session-Id → all (admin view)")
    rows = ctrl("GET", "/v2/sandboxes", expect=200).json()
    ids = [s["sandboxID"] for s in rows]
    must(SBX_A in ids and SBX_B in ids, "unscoped list contains both A and B")

    print("\n[10] edge-proxy: matching X-Session-Id → request reaches envd")
    # envd /health returns 200 with body { ok: true } once envd is up.
    deadline = time.time() + 60
    last = None
    while time.time() < deadline:
        try:
            r = httpx.get(f"https://{DOMAIN}/health",
                          headers={"Host": f"49983-{SBX_A}.{DOMAIN}",
                                   "x-access-token": TOKEN_A,
                                   "X-Session-Id": SESSION_A},
                          timeout=8, verify=HTTPS_VERIFY)
            last = r
            if r.status_code == 200:
                break
        except Exception as e:
            last = e
        time.sleep(3)
    must(last is not None and getattr(last, "status_code", None) == 200,
         f"proxy forwards matching session to envd (got {getattr(last, 'status_code', last)})")

    print("\n[11] edge-proxy: missing X-Session-Id on bound sandbox → 403")
    r = httpx.get(f"https://{DOMAIN}/health",
                  headers={"Host": f"49983-{SBX_A}.{DOMAIN}", "x-access-token": TOKEN_A},
                  timeout=8, verify=HTTPS_VERIFY)
    must(r.status_code == 403, f"edge rejects un-header'd session-bound call (got {r.status_code})")

    print("\n[12] edge-proxy: mismatched X-Session-Id → 403")
    r = httpx.get(f"https://{DOMAIN}/health",
                  headers={"Host": f"49983-{SBX_A}.{DOMAIN}",
                           "x-access-token": TOKEN_A, "X-Session-Id": SESSION_B},
                  timeout=8, verify=HTTPS_VERIFY)
    must(r.status_code == 403, f"edge rejects cross-session call (got {r.status_code})")

    print("\n[13] destroy chat-A sandbox, then re-create with same sessionId → 201")
    ctrl("DELETE", f"/sandboxes/{SBX_A}", session=SESSION_A, expect=204)
    SBX_A = None
    time.sleep(2)
    r = ctrl("POST", "/sandboxes", body={"templateID": TPL_CODE, "sessionId": SESSION_A}, expect=201)
    SBX_A = r.json()["sandboxID"]
    TOKEN_A = r.json()["envdAccessToken"]
    must(True, f"re-bind after teardown succeeds (new SBX_A={SBX_A})")

    print("\n[14] legacy: create without sessionId, GET without X-Session-Id → 200 (back-compat)")
    r = ctrl("POST", "/sandboxes", body={"templateID": TPL_CODE}, expect=201)
    legacy_sid = r.json()["sandboxID"]
    must(r.json().get("sessionID") in (None, ""),
         "legacy sandbox has no sessionID in response")
    ctrl("GET", f"/sandboxes/{legacy_sid}", expect=200)
    must(True, "legacy sandbox readable without X-Session-Id")

    # cleanup
    ctrl("DELETE", f"/sandboxes/{legacy_sid}", expect=204)
    if SBX_A:
        ctrl("DELETE", f"/sandboxes/{SBX_A}", session=SESSION_A, expect=204)
    if SBX_B:
        ctrl("DELETE", f"/sandboxes/{SBX_B}", session=SESSION_B, expect=204)

    print("\nALL SESSION-SMOKE TESTS PASSED")


def safe_cleanup():
    for sid, sess in [(SBX_A, SESSION_A), (SBX_B, SESSION_B)]:
        if not sid:
            continue
        try:
            ctrl("DELETE", f"/sandboxes/{sid}", session=sess)
        except SystemExit:
            pass


if __name__ == "__main__":
    try:
        main()
    finally:
        safe_cleanup()
