"""End-to-end smoke test for P4 第七刀 — 管理面板 (admin panel)。

覆盖:
  1. POST /admin/login 错误用户名 / 错误密码 → 401 (错误码 100012)
  2. 正确凭据 (ADMIN_USER/ADMIN_PASSWORD, 即 SBX_SSH_USER/PASSWORD) → 200,
     返回 sbxsess.<exp>.<sig> 短效 token
  3. session token 可调 GET /admin/keys / /admin/sandboxes / /health 渲染面板
  4. 伪造签名的 token → 401; 篡改 exp → 401; 过期但签名合法的 token → 401
  5. 数据面 API key 在 /admin/sandboxes 上仍被拒 (401) — 隔离语义不变
  6. ADMIN_TOKEN 直连 /admin/* 依旧可用 (向后兼容第五刀)
  7. GET /admin/ui/ 静态页免鉴权返回 200 text/html
  8. (可选, 有沙箱时) admin pause → 204 → 列表 state=paused → resume → running
  9. 通过 session token 建 key + 撤销 key 全链路可用

Run from project root:
    SBX_ADMIN_TOKEN=adm_xxx SBX_ADMIN_USER=root SBX_ADMIN_PASSWORD=123456 \
    SBX_API_KEY=e2b_xxx python -m tests.admin_panel_smoke
"""
from __future__ import annotations

import hashlib
import hmac
import os
import sys
import time

import httpx

API_URL = os.environ.get("SBX_API_URL", "http://192.168.2.162:8902")
ADMIN_TOKEN = os.environ.get("SBX_ADMIN_TOKEN", "")
ADMIN_USER = os.environ.get("SBX_ADMIN_USER", "")
ADMIN_PASSWORD = os.environ.get("SBX_ADMIN_PASSWORD", "")
DATA_KEY = os.environ.get("SBX_API_KEY", "")


def must(cond, msg):
    if not cond:
        raise SystemExit(f"✗ FAIL: {msg}")
    print(f"  ✓ {msg}")


def call(method, path, *, token=None, bearer=None, body=None, expect=None, timeout=30):
    headers = {}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["X-Admin-Token"] = token
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    r = httpx.request(method, f"{API_URL}{path}", headers=headers,
                      json=body, timeout=timeout)
    if expect is not None and r.status_code != expect:
        raise SystemExit(f"✗ {method} {path} expected {expect}, got {r.status_code}: {r.text[:200]}")
    return r


def sig_for(exp: int) -> str:
    return hmac.new(ADMIN_TOKEN.encode(), f"sbxsess.{exp}".encode(), hashlib.sha256).hexdigest()


def main():
    for name in ("SBX_ADMIN_TOKEN", "SBX_ADMIN_USER", "SBX_ADMIN_PASSWORD"):
        if not os.environ.get(name):
            sys.exit(f"[!] env {name} is required")

    print("[1] 错误凭据 → 401")
    r = call("POST", "/admin/login", body={"username": ADMIN_USER, "password": "wrong-pass"}, expect=401)
    must(r.json().get("code") == 100012, "错误密码返回 100012")
    call("POST", "/admin/login", body={"username": "no_such_user", "password": ADMIN_PASSWORD}, expect=401)
    print("  ✓ 错误用户名返回 401")

    print("[2] 正确凭据 → 200 + sbxsess token")
    r = call("POST", "/admin/login", body={"username": ADMIN_USER, "password": ADMIN_PASSWORD}, expect=200)
    sess = r.json()["token"]
    must(sess.startswith("sbxsess."), "token 前缀为 sbxsess.")
    exp = int(r.json()["expiresAt"])
    must(exp > time.time() + 7 * 3600, "有效期约 8 小时")

    print("[3] session token 访问管理端点")
    call("GET", "/admin/keys", token=sess, expect=200)
    print("  ✓ GET /admin/keys 200")
    r = call("GET", "/admin/sandboxes", token=sess, expect=200)
    must("sandboxes" in r.json(), "GET /admin/sandboxes 返回 sandboxes 数组")

    print("[4] 篡改/伪造/过期 token → 401")
    forged = f"sbxsess.{exp}.{'00' * 32}"
    r = call("GET", "/admin/keys", token=forged, expect=401)
    must(r.json().get("code") == 100012, "伪造签名返回 100012")
    tampered_exp = exp + 3600
    call("GET", "/admin/keys", token=f"sbxsess.{tampered_exp}.{sig_for(exp)}", expect=401)
    print("  ✓ 篡改 exp 返回 401")
    past = int(time.time()) - 10
    call("GET", "/admin/keys", token=f"sbxsess.{past}.{sig_for(past)}", expect=401)
    print("  ✓ 合法签名但已过期返回 401")

    print("[5] 数据面 key 在 admin 入口被拒")
    if DATA_KEY:
        r = call("GET", "/admin/sandboxes", bearer=DATA_KEY, expect=401)
        must(r.json().get("code") == 100012, "数据面 key → 401 (隔离保持)")
    else:
        print("  - 跳过 (未设置 SBX_API_KEY)")

    print("[6] ADMIN_TOKEN 直连仍可用")
    call("GET", "/admin/keys", token=ADMIN_TOKEN, expect=200)
    print("  ✓ 向后兼容第五刀")

    print("[7] /admin/ui 静态页免登录")
    r = httpx.get(f"{API_URL}/admin/ui/", timeout=30)
    must(r.status_code == 200, f"GET /admin/ui/ → {r.status_code}")
    must("text/html" in r.headers.get("content-type", ""), "Content-Type 为 text/html")
    must("管理面板" in r.text, "页面包含面板标题")

    print("[8] 沙箱 pause/resume (仅当存在 running 沙箱)")
    rows = call("GET", "/admin/sandboxes", token=sess, expect=200).json()["sandboxes"]
    running = [s for s in rows if s["state"] == "running"]
    if running:
        sid = running[0]["sandboxID"]
        call("POST", f"/admin/sandboxes/{sid}/pause", token=sess, body={}, expect=204)
        after = call("GET", "/admin/sandboxes", token=sess, expect=200).json()["sandboxes"]
        st = next(s["state"] for s in after if s["sandboxID"] == sid)
        must(st == "paused", f"{sid} 已暂停 (state={st})")
        r = call("POST", f"/admin/sandboxes/{sid}/resume", token=sess,
                 body={"timeout": 300}, expect=200, timeout=90)
        must(r.json()["state"] == "running", f"{sid} 已恢复")
        must("envdAccessToken" not in r.json(), "admin 视图不返回 envd token")
    else:
        print("  - 无 running 沙箱，跳过 (生命周期已由其它 smoke 覆盖)")

    print("[9] session token 建 key → 撤销 全链路")
    r = call("POST", "/admin/keys", token=sess,
             body={"owner": "panel-smoke", "tenant": "panel-smoke", "label": "第七刀smoke"}, expect=201)
    kid = r.json()["meta"]["id"]
    plain = r.json()["key"]
    must(plain.startswith("e2b_"), "新 key 为 e2b_ 前缀明文 (一次性返回)")
    ok = httpx.get(f"{API_URL}/v2/templates", headers={"Authorization": f"Bearer {plain}"}, timeout=30)
    must(ok.status_code == 200, "新 key 立即可在数据面鉴权")
    call("DELETE", f"/admin/keys/{kid}", token=sess, expect=200)
    dead = httpx.get(f"{API_URL}/v2/templates", headers={"Authorization": f"Bearer {plain}"}, timeout=30)
    must(dead.status_code == 401, "撤销立即生效")

    print(f"\nALL PASS — admin panel smoke ({API_URL})")


if __name__ == "__main__":
    main()
