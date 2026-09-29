"""End-to-end smoke test for P3 Ingress Keepalive.

Exercises the edge-proxy "wake paused sandbox on connection refusal" behavior:

  1. Create sandbox → envd is reachable via edge proxy
  2. Pause sandbox → data plane stopped
  3. Make a request via edge proxy → first one may pay the wake-up cost (~3-25s)
     → eventually returns 200 (envd auto-resumed by edge proxy)
  4. Subsequent request → fast path (sandbox is running again, no wake)
  5. Pause again → second request still auto-resumes (verifies the wake-once-per-pause model)

Run from project root:
    python -m tests.keepalive_smoke

Required env:
  SBX_API_URL    control plane base URL (default http://127.0.0.1:8902)
  SBX_API_KEY    control plane API key
  SBX_DOMAIN     edge proxy domain (e.g. 192.168.2.162.nip.io)
  SBX_CA_CERT    path to edge-proxy CA certificate (recommended)
  SBX_INSECURE   set to 1 to skip TLS verification
"""
from __future__ import annotations

import os
import time
from typing import Union

import httpx

API_URL = os.environ.get("SBX_API_URL", "http://127.0.0.1:8902")
API_KEY = os.environ["SBX_API_KEY"]
# Use SBX_DOMAIN if it resolves, otherwise fall back to SBX_EDGE_HOST (raw IP/host).
# The latter is useful when running the smoke from inside the sandbox host itself
# (nip.io may not be DNS-resolvable in some environments).
DOMAIN = os.environ.get("SBX_DOMAIN") or os.environ["SBX_EDGE_HOST"]
HEADERS = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}

# TLS for edge proxy (self-signed). If SBX_INSECURE=1, skip verification entirely.
# Otherwise point httpx at SBX_CA_CERT (path to the edge-proxy CA).
if os.environ.get("SBX_INSECURE") == "1":
    HTTPS_VERIFY: Union[bool, str] = False
else:
    ca = os.environ.get("SBX_CA_CERT")
    HTTPS_VERIFY = ca if ca and os.path.isfile(ca) else True


def must_ok(cond, msg):
    if not cond:
        raise SystemExit(f"✗ FAIL: {msg}")
    print(f"  ✓ {msg}")


def http_call(method, path, body=None, expect=None, **kw):
    r = httpx.request(method, f"{API_URL}{path}", headers=HEADERS, json=body, timeout=60, **kw)
    if expect is not None and r.status_code != expect:
        raise SystemExit(f"✗ {method} {path} expected {expect}, got {r.status_code}: {r.text[:200]}")
    return r


def pick_template() -> str:
    rows = http_call("GET", "/v2/templates", expect=200).json()
    if not rows:
        raise SystemExit("no templates; create one first")
    # Prefer code-interpreter (no browser overhead); fall back to any
    for r in rows:
        if "interpreter" in r.get("name", "").lower():
            return r["templateCode"]
    return rows[0]["templateCode"]


def create_sandbox(template_code: str) -> str:
    r = http_call("POST", "/sandboxes", body={"templateID": template_code, "timeout": 600}, expect=201)
    return r.json()["sandboxID"]


def kill_sandbox(sid: str):
    httpx.delete(f"{API_URL}/sandboxes/{sid}", headers=HEADERS, timeout=30)


ENVD_CONTAINER_PORT = 49983  # port number used in the subdomain


def envd_health_via_edge(sandbox_id: str, timeout_s: float = 60.0):
    """Hit edge-proxy HTTPS route to the sandbox's envd /health.

    Uses the bare IP + a Host header (since we're talking to a vhost that needs
    the `${port}-${sbxid}.${domain}` host header to route correctly).
    """
    # Split DOMAIN into base host (for SNI/TLS) and suffix.
    # DOMAIN may be an IP like "192.168.2.162" or a hostname like "x.nip.io".
    host_header = f"{ENVD_CONTAINER_PORT}-{sandbox_id}.{DOMAIN}"
    url = f"https://{DOMAIN}/health"
    t0 = time.time()
    r = httpx.get(url, headers={"Host": host_header}, timeout=timeout_s, verify=HTTPS_VERIFY)
    return r.status_code, time.time() - t0


def get_state(sandbox_id: str) -> str:
    r = http_call("GET", f"/sandboxes/{sandbox_id}", expect=200).json()
    return r["state"]


def pause_sandbox(sandbox_id: str, criu: str = "false"):
    """Pause via plain docker stop. CRIU is skipped because the production
    sandbox image can't actually checkpoint cleanly (known issue with the
    containerd snapshotter on Docker 29) — even when CRIU returns 201, the
    container keeps running. For the keepalive test we need the container
    really stopped, so we bypass CRIU here.
    """
    http_call("POST", f"/sandboxes/{sandbox_id}/pause", body={"criu": criu}, expect=204)


def main():
    template = pick_template()
    print(f"using template: {template}")

    # Create sandbox
    sid = create_sandbox(template)
    print(f"  → sandbox {sid}")
    http_call("GET", f"/sandboxes/{sid}", expect=200)
    print(f"  → envd subdomain uses container port {ENVD_CONTAINER_PORT}, edge-domain={DOMAIN}")

    try:
        # ---- 1. Edge proxy reachable while sandbox is running ----
        print("\n[1] edge proxy reaches running sandbox")
        sc, dt = envd_health_via_edge(sid)
        must_ok(sc == 200, f"first /health returns 200 (got {sc} in {dt:.2f}s)")

        # ---- 2. Pause sandbox, then probe via edge proxy → auto resume ----
        print("\n[2] paused sandbox wakes on first edge-proxy request")
        pause_sandbox(sid)
        must_ok(get_state(sid) == "paused", "state=paused")
        # immediate probe: edge proxy sees ECONNREFUSED, calls /internal/auto-resume, retries
        sc, dt = envd_health_via_edge(sid, timeout_s=60)
        must_ok(sc == 200, f"first post-pause request succeeds (200 in {dt:.2f}s)")
        must_ok(get_state(sid) == "running", "state=running after wake")
        # second probe should be fast (no wake needed)
        sc2, dt2 = envd_health_via_edge(sid)
        must_ok(sc2 == 200, f"second probe also 200 in {dt2:.2f}s (fast path)")
        must_ok(dt2 < dt, f"fast path {dt2:.2f}s < wake path {dt:.2f}s")

        # ---- 3. Pause again, second wake cycle ----
        print("\n[3] second wake cycle (verify wake-up is repeatable)")
        pause_sandbox(sid)
        must_ok(get_state(sid) == "paused", "state=paused again")
        sc, dt = envd_health_via_edge(sid, timeout_s=60)
        must_ok(sc == 200, f"second wake also succeeds ({dt:.2f}s)")
        must_ok(get_state(sid) == "running", "state=running after second wake")

        # ---- 4. Control plane sanity: /internal/auto-resume on running is a no-op ----
        print("\n[4] /internal/auto-resume idempotent on running sandbox")
        r = httpx.post(f"{API_URL}/internal/auto-resume",
                       params={"sandbox": sid, "port": ENVD_CONTAINER_PORT}, timeout=30)
        must_ok(r.status_code == 200 and r.json().get("alreadyRunning") is True,
                f"already-running short-circuit (got {r.status_code} {r.text[:100]})")

    finally:
        kill_sandbox(sid)

    print("\nALL KEEPALIVE SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()