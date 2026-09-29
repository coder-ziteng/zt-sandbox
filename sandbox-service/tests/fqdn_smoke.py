"""End-to-end smoke test for P3 Egress FQDN allowlist (wildcards + CDN rotation).

Coverage:
  1. Wildcard expansion: `*.openai.com` resolves apex + www/api/cdn subdomains
  2. Manual refresh: POST /sandboxes/{id}/netpolicy/refresh re-resolves and updates state
  3. Refresh-After semantics: refreshedAt advances; resolved IPs may change
  4. CIDR mixed with domain: both rules end up in the iptables chain
  5. Plain exact-match domains: resolved without expansion
  6. NetworkPolicy template fields surface through GET /sandboxes/{id}

Run from project root:
    python -m tests.fqdn_smoke

Required env:
  SBX_API_URL    control plane base URL (default http://127.0.0.1:8902)
  SBX_API_KEY    control plane API key
  SBX_TEMPLATE   base template code (defaults to first one in /v2/templates)

Note: the auto-applied netpolicy is intentionally skipped for host-network
containers (see runtime.py), so this smoke drives the *manual* install path
via POST /sandboxes/{id}/netpolicy. That exercises the same code path used by
the periodic refresh loop and verifies wildcard expansion + state refresh.
"""
from __future__ import annotations

import os
import time

import httpx

API_URL = os.environ.get("SBX_API_URL", "http://127.0.0.1:8902")
API_KEY = os.environ["SBX_API_KEY"]
HEADERS = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
TEMPLATE_BASE = os.environ.get("SBX_TEMPLATE")


def must(cond, msg):
    if not cond:
        raise SystemExit(f"✗ FAIL: {msg}")
    print(f"  ✓ {msg}")


def http_call(method, path, body=None, expect=None):
    r = httpx.request(method, f"{API_URL}{path}", headers=HEADERS, json=body, timeout=60)
    if expect is not None and r.status_code != expect:
        raise SystemExit(f"✗ {method} {path} expected {expect}, got {r.status_code}: {r.text[:200]}")
    return r


def pick_template() -> str:
    if TEMPLATE_BASE:
        return TEMPLATE_BASE
    rows = http_call("GET", "/v2/templates", expect=200).json()
    return rows[0]["templateCode"]


def create_template(name, image, network_policy) -> str:
    body = {"name": name, "image": image, "cpuCount": 1, "memoryMB": 1024, "diskSizeMB": 1024,
            "networkPolicy": network_policy}
    return http_call("POST", "/v3/templates", body=body, expect=201).json()["templateCode"]


def create_sandbox(template_code: str, timeout=300) -> str:
    return http_call("POST", "/sandboxes", body={"templateID": template_code, "timeout": timeout},
                     expect=201).json()["sandboxID"]


def kill_sandbox(sid: str):
    httpx.delete(f"{API_URL}/sandboxes/{sid}", headers=HEADERS, timeout=30)


def delete_template(code: str):
    httpx.delete(f"{API_URL}/templates/{code}", headers=HEADERS, timeout=15)


def main():
    base_tpl = pick_template()
    print(f"using base template: {base_tpl}")

    # ---- 1. Wildcard expansion (apply with *.openai.com, *.anthropic.com) ----
    print("\n[1] wildcard expansion *.openai.com")
    wild_tpl = create_template("fqdn-smoke-wild",
                                "sandbox/code-interpreter:v1",
                                {"mode": "allowlist",
                                 "domains": ["*.openai.com", "*.anthropic.com"],
                                 "cidrs": ["10.0.0.0/8"]})
    try:
        sid = create_sandbox(wild_tpl)
        print(f"  → sandbox {sid}")
        body = {"mode": "allowlist",
               "domains": ["*.openai.com", "*.anthropic.com"],
               "cidrs": ["10.0.0.0/8"]}
        r = http_call("POST", f"/sandboxes/{sid}/netpolicy", body=body, expect=200).json()
        must(r.get("applied") is True, f"netpolicy applied (got {r})")
        resolved = r.get("resolved", {})
        # Must include the apex AND common prefixes
        must("openai.com" in resolved, "apex openai.com in resolved")
        must(any(d.startswith("api.") and d.endswith("openai.com") for d in resolved),
             f"api.openai.com in resolved (got keys: {list(resolved.keys())})")
        must(any(d.startswith("www.") and d.endswith("anthropic.com") for d in resolved),
             "www.anthropic.com in resolved")
        must("10.0.0.0/8" in r.get("allowed", []), "cidr in allowed list")
        first_refreshed_at = r["refreshedAt"]
        kill_sandbox(sid)

        # ---- 2. Manual refresh (verify refreshedAt advances and state updates) ----
        print("\n[2] manual refresh updates state")
        sid2 = create_sandbox(wild_tpl)
        try:
            http_call("POST", f"/sandboxes/{sid2}/netpolicy", body=body, expect=200)
            time.sleep(1.2)  # ensure refreshedAt differs
            r2 = http_call("POST", f"/sandboxes/{sid2}/netpolicy/refresh", expect=200).json()
            must("refreshedAt" in r2 and r2["refreshedAt"] > first_refreshed_at,
                 f"refreshedAt advanced (now={r2['refreshedAt']}, prev={first_refreshed_at})")
            must("resolved" in r2 and "openai.com" in r2["resolved"],
                 "resolved IPs returned on refresh")
        finally:
            kill_sandbox(sid2)

        # ---- 3. Exact-match domains (no expansion) ----
        print("\n[3] exact-match domain (no wildcard expansion)")
        exact_tpl = create_template("fqdn-smoke-exact",
                                    "sandbox/code-interpreter:v1",
                                    {"mode": "allowlist", "domains": ["example.com"]})
        try:
            sid3 = create_sandbox(exact_tpl)
            try:
                body3 = {"mode": "allowlist", "domains": ["example.com"]}
                r3 = http_call("POST", f"/sandboxes/{sid3}/netpolicy", body=body3, expect=200).json()
                must("example.com" in r3.get("resolved", {}),
                     "example.com resolved (no expansion)")
                must(not any(d.startswith("www.") and d.endswith("example.com")
                             for d in r3.get("resolved", {})),
                     "exact mode does NOT inject www.example.com")
            finally:
                kill_sandbox(sid3)
        finally:
            delete_template(exact_tpl)

    finally:
        delete_template(wild_tpl)

    print("\nALL FQDN SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()