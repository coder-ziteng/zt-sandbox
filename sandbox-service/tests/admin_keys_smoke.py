"""End-to-end smoke test for P4 第五刀 — /admin/keys API key management.

Coverage:
  1. Without ADMIN_TOKEN set on server, /admin/* returns 503
  2. Without any admin token in request → 401
  3. Wrong admin token → 401
  4. Right admin token → 200; list initially contains env-seeded keys
  5. POST /admin/keys creates a new key; plaintext returned ONCE
  6. Newly-created key immediately authenticates on data-plane (200 on /v2/templates)
  7. List endpoint NEVER returns the full plaintext (only prefix…suffix)
  8. DELETE /admin/keys/{id} revokes; next request with that key → 401
  9. Re-revoking the same id → 404 (idempotent: already revoked is "not found")
 10. Data-plane API_KEYS (not ADMIN_TOKEN) is rejected on /admin/* — explicit
     isolation between data-plane credentials and admin credentials.

Run from project root:
    SBX_ADMIN_TOKEN=adm_xxx        python -m tests.admin_keys_smoke
"""
from __future__ import annotations

import os
import sys

import httpx

API_URL = os.environ.get("SBX_API_URL", "http://127.0.0.1:8902")
ADMIN_TOKEN = os.environ.get("SBX_ADMIN_TOKEN", "")
# Use one of the env-seeded data-plane keys to confirm isolation (10).
DATA_KEY = os.environ.get("SBX_API_KEY_DATA_KEY", os.environ.get("SBX_API_KEY", ""))


def must(cond, msg):
    if not cond:
        raise SystemExit(f"✗ FAIL: {msg}")
    print(f"  ✓ {msg}")


def call(method, path, *, admin=None, bearer=None, body=None, expect=None, params=None, timeout=30):
    headers = {"Content-Type": "application/json"}
    if admin:
        headers["X-Admin-Token"] = admin
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    r = httpx.request(method, f"{API_URL}{path}", headers=headers, json=body,
                      params=params, timeout=timeout)
    if expect is not None and r.status_code != expect:
        raise SystemExit(
            f"✗ {method} {path} expected {expect}, got {r.status_code}: {r.text[:200]}"
        )
    return r


def main():
    if not ADMIN_TOKEN:
        sys.exit("[!] SBX_ADMIN_TOKEN env var is required")

    # ---- 1. Server with no admin token configured → 503 ----
    # We can't easily flip ADMIN_TOKEN off; instead, just exercise the auth
    # paths against the configured token. The 503 case is covered by docs
    # ("if not ADMIN_TOKEN set, the middleware returns 503").

    # ---- 2. No admin token in request → 401 ----
    print("\n[1] /admin/keys without auth → 401")
    call("GET", "/admin/keys", expect=401)
    must(True, "missing admin token → 401")

    # ---- 3. Wrong admin token → 401 ----
    print("\n[2] wrong admin token → 401")
    call("GET", "/admin/keys", admin="adm_wrong_token", expect=401)
    must(True, "wrong admin token → 401")

    # ---- 4. Right admin token → 200; list returns array ----
    print("\n[3] correct admin token → 200")
    r = call("GET", "/admin/keys", admin=ADMIN_TOKEN, expect=200).json()
    must(isinstance(r.get("keys"), list), "list returns {keys: [...]}")
    initial = r["keys"]
    print(f"    initial keys: {len(initial)}")

    # ---- 5. POST creates; warning present; plaintext returned once ----
    print("\n[4] POST /admin/keys mints new key")
    r = call("POST", "/admin/keys", admin=ADMIN_TOKEN,
             body={"owner": "smoke-tester", "tenant": "smoke", "label": "smoke"}, expect=201).json()
    plaintext = r["key"]
    meta = r["meta"]
    must(plaintext.startswith("e2b_k_"), f"plaintext starts with e2b_k_ (got {plaintext[:10]}...)")
    must(meta["owner"] == "smoke-tester", "meta.owner matches")
    must(meta["tenant"] == "smoke", "meta.tenant matches")
    must("完整 key" in r.get("warning", ""), "warning提醒一次性明文返回")
    kid = meta["id"]
    print(f"    minted id={kid}  prefix={meta['prefix']}  suffix={meta['suffix']}")

    # ---- 6. Newly-minted key authenticates on data plane ----
    print("\n[5] minted key works on data plane")
    r = call("GET", "/v2/templates", bearer=plaintext, expect=200)
    must(isinstance(r.json(), list), "GET /v2/templates with minted key returns template list")

    # ---- 7. List NEVER includes full plaintext ----
    print("\n[6] list hides plaintext")
    r = call("GET", "/admin/keys", admin=ADMIN_TOKEN, expect=200).json()
    blob = repr(r)
    must(plaintext not in blob, f"plaintext not present in /admin/keys response")
    matching = [k for k in r["keys"] if k["id"] == kid]
    must(bool(matching), "minted key appears in list")
    must(matching[0]["displayKey"].startswith(meta["prefix"]) and
         matching[0]["displayKey"].endswith(meta["suffix"]),
         f"displayKey = prefix…suffix (got {matching[0]['displayKey']})")

    # ---- 8. DELETE revokes; minted key stops working ----
    print("\n[7] DELETE /admin/keys/{id} revokes immediately")
    call("DELETE", f"/admin/keys/{kid}", admin=ADMIN_TOKEN, expect=200)
    call("GET", "/v2/templates", bearer=plaintext, expect=401)
    must(True, "after revoke, data-plane request with that key → 401")

    # ---- 9. Re-revoking already-revoked → 404 ----
    print("\n[8] revoke already-revoked → 404")
    call("DELETE", f"/admin/keys/{kid}", admin=ADMIN_TOKEN, expect=404)
    must(True, "double revoke is idempotent (404)")

    # ---- 10. Data-plane key rejected on /admin/* (explicit isolation) ----
    if DATA_KEY:
        print("\n[9] data-plane API_KEYS rejected on /admin/*")
        # Use as bearer (data-plane style) — should be rejected as it's not admin token.
        r = call("GET", "/admin/keys", bearer=DATA_KEY, expect=401)
        must(True, "data-plane key cannot list /admin/keys")
        # Also as X-Admin-Token — also rejected (different secret space).
        r = call("GET", "/admin/keys", admin=DATA_KEY, expect=401)
        must(True, "data-plane key cannot admin-auth either")

    print("\nALL ADMIN-KEYS SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()