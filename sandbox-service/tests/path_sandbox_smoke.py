"""End-to-end smoke test for P4 第四刀 — 容器内文件路径沙箱 (/workspace + /tmp).

Coverage (drives envd data plane via the edge-proxy subdomain route):
  1. Write / read inside /workspace succeeds (200 / 200)
  2. Relative path "foo.txt" resolves to /workspace/foo.txt
  3. Read /etc/passwd → 403 (system path blocked)
  4. Read /root/.bashrc → 403 (user system path blocked)
  5. Path traversal ../../etc/passwd → 403 (resolves outside sandbox)
  6. Read /proc/1/status → 403 (/proc blocked)
  7. Write to /tmp/foo → allowed (temp scratch)
  8. MakeDir /workspace/dir → 200
  9. Stat /workspace/dir → 200
 10. ListDir /workspace → returns entries (depth 1)
 11. ListDir / → 403 (top-level blocked)
 12. Remove /workspace/dir → 200
 13. Move /workspace/foo → /workspace/bar → 200
 14. Move /workspace/foo → /etc/foo → 403 (dest outside sandbox)

Run from project root:
    python -m tests.path_sandbox_smoke

Required env:
  SBX_API_URL    control plane base URL (default http://127.0.0.1:8902)
  SBX_API_KEY    control plane API key
  SBX_DOMAIN     edge proxy domain (e.g. 192.168.2.162.nip.io) or SBX_EDGE_HOST
  SBX_CA_CERT    path to edge-proxy CA certificate (recommended)
  SBX_INSECURE   set to 1 to skip TLS verification
"""
from __future__ import annotations

import os
from typing import Union

import httpx

API_URL = os.environ.get("SBX_API_URL", "http://127.0.0.1:8902")
API_KEY = os.environ["SBX_API_KEY"]
DOMAIN = os.environ.get("SBX_DOMAIN") or os.environ["SBX_EDGE_HOST"]
HEADERS = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}

# TLS for edge proxy (self-signed). If SBX_INSECURE=1, skip verification entirely.
if os.environ.get("SBX_INSECURE") == "1":
    HTTPS_VERIFY: Union[bool, str] = False
else:
    ca = os.environ.get("SBX_CA_CERT")
    HTTPS_VERIFY = ca if ca and os.path.isfile(ca) else True


def must(cond, msg):
    if not cond:
        raise SystemExit(f"✗ FAIL: {msg}")
    print(f"  ✓ {msg}")


def call(method, path, headers, body=None, expect=None, params=None, timeout=30):
    r = httpx.request(method, f"{API_URL}{path}", headers=headers, json=body,
                      params=params, timeout=timeout)
    if expect is not None and r.status_code != expect:
        raise SystemExit(
            f"✗ {method} {path} expected {expect}, got {r.status_code}: {r.text[:200]}"
        )
    return r


def get_sandbox_meta(sid: str) -> dict:
    return call("GET", f"/sandboxes/{sid}", HEADERS, expect=200).json()


def envd_call(method: str, path: str, token: str, sandbox_id: str,
              params=None, body=None, expect=None, content_type="application/json"):
    """Call envd via the edge-proxy subdomain route.

    URL pattern: `https://{DOMAIN}{path}` with `Host: 49983-{sid}.{DOMAIN}`
    """
    host_header = f"49983-{sandbox_id}.{DOMAIN}"
    url = f"https://{DOMAIN}{path}"
    h = {"Host": host_header, "x-access-token": token, "Content-Type": content_type}
    r = httpx.request(method, url, headers=h, params=params, content=body,
                      timeout=15, verify=HTTPS_VERIFY)
    if expect is not None and r.status_code != expect:
        raise SystemExit(
            f"✗ envd {method} {path} expected {expect}, got {r.status_code}: {r.text[:200]}"
        )
    return r


def pick_template() -> str:
    rows = call("GET", "/v2/templates", HEADERS, expect=200).json()
    for r in rows:
        if "interpreter" in r.get("name", "").lower():
            return r["templateCode"]
    return rows[0]["templateCode"]


def create_sandbox() -> tuple[str, str]:
    template = pick_template()
    sid = call("POST", "/sandboxes", HEADERS, body={"templateID": template, "timeout": 600},
               expect=201).json()["sandboxID"]
    token = get_sandbox_meta(sid)["envdAccessToken"]
    return sid, token


def main():
    print("[setup] creating sandbox")
    sid, token = create_sandbox()
    print(f"  → sandbox={sid} via edge {DOMAIN}")

    def env(method, path, expect=None, params=None, body=None):
        return envd_call(method, path, token, sid, params=params, body=body, expect=expect)

    try:
        # ---- 1. Write / read inside /workspace ----
        print("\n[1] /workspace/foo.txt round-trip")
        env("POST", "/files?path=/workspace/foo.txt", body=b"hello sandbox", expect=200)
        r = env("GET", "/files?path=/workspace/foo.txt", expect=200)
        must(r.content == b"hello sandbox", "wrote/read /workspace/foo.txt returns 'hello sandbox'")

        # ---- 2. Relative path resolves to /workspace ----
        print("\n[2] relative path resolves under /workspace")
        env("POST", "/files?path=rel.txt", body=b"relative", expect=200)
        r = env("GET", "/files?path=rel.txt", expect=200)
        must(r.content == b"relative", "read 'rel.txt' returns 'relative' (resolved to /workspace/rel.txt)")
        r = env("GET", "/files?path=/workspace/rel.txt", expect=200)
        must(r.content == b"relative", "read '/workspace/rel.txt' returns same content")

        # ---- 3. /etc/passwd blocked ----
        print("\n[3] /etc/passwd blocked")
        env("GET", "/files?path=/etc/passwd", expect=403)
        must(True, "GET /etc/passwd → 403")

        # ---- 4. /root blocked ----
        print("\n[4] /root/.bashrc blocked")
        env("GET", "/files?path=/root/.bashrc", expect=403)
        env("GET", "/files?path=/root", expect=403)
        must(True, "GET /root/.bashrc and /root → 403")

        # ---- 5. Path traversal blocked ----
        print("\n[5] path traversal ../../etc/passwd blocked")
        env("GET", "/files?path=foo/../../etc/passwd", expect=403)
        env("POST", "/files?path=foo/../../etc/evil", body=b"x", expect=403)
        must(True, "traversal attempts → 403")

        # ---- 6. /proc blocked ----
        print("\n[6] /proc blocked")
        env("GET", "/files?path=/proc/1/status", expect=403)
        must(True, "GET /proc/1/status → 403")

        # ---- 7. /tmp allowed (scratch space) ----
        print("\n[7] /tmp/foo allowed")
        env("POST", "/files?path=/tmp/foo", body=b"tmp data", expect=200)
        r = env("GET", "/files?path=/tmp/foo", expect=200)
        must(r.content == b"tmp data", "round-trip via /tmp works")

        # ---- 8. MakeDir ----
        print("\n[8] MakeDir /workspace/mydir")
        r = env("POST", "/filesystem.Filesystem/MakeDir?path=/workspace/mydir",
                body=b'{"path":"/workspace/mydir"}',
                expect=200)
        must(True, f"MakeDir returns 200 (got {r.text[:80]})")

        # ---- 9. Stat ----
        print("\n[9] Stat /workspace/mydir")
        env("POST", "/filesystem.Filesystem/Stat?path=/workspace/mydir",
            body=b'{"path":"/workspace/mydir"}',
            expect=200)
        must(True, "Stat returns 200")

        # ---- 10. ListDir /workspace returns entries ----
        print("\n[10] ListDir /workspace lists entries")
        r = env("POST", "/filesystem.Filesystem/ListDir?path=/workspace&depth=1",
                body=b'{"path":"/workspace","depth":1}',
                expect=200)
        body = r.text
        must("foo.txt" in body and "rel.txt" in body and "mydir" in body,
             f"listdir /workspace contains foo.txt/rel.txt/mydir (got {body[:200]})")

        # ---- 11. ListDir / blocked ----
        print("\n[11] ListDir / blocked")
        env("POST", "/filesystem.Filesystem/ListDir?path=/",
            body=b'{"path":"/"}',
            expect=403)
        must(True, "ListDir / → 403")

        # ---- 12. Remove /workspace/mydir ----
        print("\n[12] Remove /workspace/mydir")
        env("POST", "/filesystem.Filesystem/Remove?path=/workspace/mydir",
            body=b'{"path":"/workspace/mydir"}',
            expect=200)
        must(True, "Remove returns 200")

        # ---- 13. Move within /workspace ----
        print("\n[13] Move /workspace/foo.txt → /workspace/bar.txt")
        env("POST", "/filesystem.Filesystem/Move",
            body=b'{"source":"/workspace/foo.txt","destination":"/workspace/bar.txt"}',
            expect=200)
        env("GET", "/files?path=/workspace/bar.txt", expect=200)
        env("GET", "/files?path=/workspace/foo.txt", expect=404)
        must(True, "move within workspace works; old path 404s")

        # ---- 14. Move outside /workspace blocked ----
        print("\n[14] Move /workspace/bar.txt → /etc/foo blocked")
        env("POST", "/filesystem.Filesystem/Move",
            body=b'{"source":"/workspace/bar.txt","destination":"/etc/foo"}',
            expect=403)
        must(True, "move with destination outside sandbox → 403")

    finally:
        if os.environ.get("SBX_KEEP") != "1":
            call("DELETE", f"/sandboxes/{sid}", HEADERS, expect=204)

    print("\nALL PATH-SANDBOX SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
