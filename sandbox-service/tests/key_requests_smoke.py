"""End-to-end smoke test for P4 第八刀 — API Key 自助申请审批流。

覆盖:
  1. POST /keys/requests 免鉴权 → 201, 一次性返回 requestID + ticket
  2. 缺 applicant/owner/tenant → 400 (100013)
  3. GET /keys/requests/{rid}: 错误 ticket → 403 (100017); 正确 → pending
  4. 未批准先 claim → 409
  5. /admin/key-requests 可见 pending 申请, 且不泄露 ticket_hash/明文 outbox
  6. approve → 200 返回 issuedKeyID 但绝无 key 明文; 重复 approve → 409
  7. claim → 200 一次性明文; 二次 claim → 410
  8. 领取到的 key 立即可在数据面鉴权; 撤销后 401
  9. reject (带理由) → 申请人可查到 rejected + rejectReason; reject 后 claim → 409
 10. DELETE 撤回 pending 申请 → cancelled; 撤回后 approve → 409

限流 (100016) 与 owner pending 上限不实测 — 会烧掉本 IP 的窗口配额, 影响后续
run; 逻辑已由 store/端点代码审查覆盖。

Run from project root:
    SBX_ADMIN_TOKEN=adm_xxx python -m tests.key_requests_smoke
"""
from __future__ import annotations

import os
import sys
import time

import httpx

API_URL = os.environ.get("SBX_API_URL", "http://192.168.2.162:8902")
ADMIN_TOKEN = os.environ.get("SBX_ADMIN_TOKEN", "")
OWNER = f"kr-smoke-{int(time.time())}"


def must(cond, msg):
    if not cond:
        raise SystemExit(f"✗ FAIL: {msg}")
    print(f"  ✓ {msg}")


def call(method, path, *, ticket=None, body=None, admin=False, expect=None, timeout=30):
    headers = {}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if ticket:
        headers["X-Request-Ticket"] = ticket
    if admin:
        headers["X-Admin-Token"] = ADMIN_TOKEN
    r = httpx.request(method, f"{API_URL}{path}", headers=headers, json=body, timeout=timeout)
    if expect is not None and r.status_code != expect:
        raise SystemExit(f"✗ {method} {path} expected {expect}, got {r.status_code}: {r.text[:200]}")
    return r


def submit(tenant="t1", label="第八刀smoke", note="自动化冒烟"):
    r = call("POST", "/keys/requests",
             body={"applicant": "key-requests-smoke", "owner": OWNER,
                   "tenant": tenant, "label": label, "note": note},
             expect=201)
    j = r.json()
    must(j.get("requestID", "").startswith("kreq") and j.get("ticket") and j.get("status") == "pending",
         "201 返回 requestID/ticket/pending")
    return j["requestID"], j["ticket"]


def main():
    if not ADMIN_TOKEN:
        sys.exit("[!] env SBX_ADMIN_TOKEN is required")

    print("[1] 免鉴权提交申请 → 201")
    rid, ticket = submit()

    print("[2] 缺字段 → 400 (100013)")
    r = call("POST", "/keys/requests", body={"owner": OWNER}, expect=400)
    must(r.json().get("code") == 100013, "缺 applicant/tenant 返回 100013")

    print("[3] 凭 ticket 查状态")
    r = call("GET", f"/keys/requests/{rid}", ticket="wrong-ticket", expect=403)
    must(r.json().get("code") == 100017, "错误 ticket → 100017")
    r = call("GET", f"/keys/requests/{rid}", ticket=ticket, expect=200)
    j = r.json()
    must(j["status"] == "pending", "正确 ticket → pending")
    must("ticket" not in j and "ticketHash" not in j, "状态查询不回显 ticket")

    print("[4] 未批准先 claim → 409")
    call("POST", f"/keys/requests/{rid}/claim", ticket=ticket, expect=409)
    print("  ✓ pending 状态无明文可领")

    print("[5] admin 列表可见且无敏感字段")
    r = call("GET", "/admin/key-requests?status=pending", admin=True, expect=200)
    rows = [x for x in r.json()["requests"] if x["requestID"] == rid]
    must(len(rows) == 1, "pending 列表含本申请")
    raw = str(r.json())
    must(ticket not in raw and "issued_key_plain" not in raw and "ticket_hash" not in raw,
         "列表不泄露 ticket 与明文 outbox")

    print("[6] approve → 只回 issuedKeyID; 重复 approve → 409")
    r = call("POST", f"/admin/key-requests/{rid}/approve", admin=True, body={}, expect=200)
    kid = r.json()["issuedKeyID"]
    must("key" not in r.json(), "approve 响应不含 key 明文")
    call("POST", f"/admin/key-requests/{rid}/approve", admin=True, body={}, expect=409)
    print("  ✓ 二次审批被状态守卫拒绝")

    print("[7] claim 一次性领取")
    r = call("POST", f"/keys/requests/{rid}/claim", ticket=ticket, expect=200)
    plain = r.json()["key"]
    must(plain.startswith("e2b_"), "领取到 e2b_ 前缀明文 key")
    call("POST", f"/keys/requests/{rid}/claim", ticket=ticket, expect=410)
    print("  ✓ 二次 claim → 410 (明文已清除)")

    print("[8] 新 key 数据面可用 → 撤销后失效")
    ok = httpx.get(f"{API_URL}/v2/templates", headers={"Authorization": f"Bearer {plain}"}, timeout=30)
    must(ok.status_code == 200, "新 key 立即通过数据面鉴权")
    call("DELETE", f"/admin/keys/{kid}", admin=True, expect=200)
    dead = httpx.get(f"{API_URL}/v2/templates", headers={"Authorization": f"Bearer {plain}"}, timeout=30)
    must(dead.status_code == 401, "撤销后 401")

    print("[9] reject 全链路")
    rid2, ticket2 = submit(tenant="t2", label="驳回样本")
    r = call("POST", f"/admin/key-requests/{rid2}/reject", admin=True,
             body={"reason": "owner 无法核实"}, expect=200)
    must(r.json()["status"] == "rejected", "驳回成功")
    r = call("GET", f"/keys/requests/{rid2}", ticket=ticket2, expect=200)
    must(r.json()["rejectReason"] == "owner 无法核实", "申请人可见驳回理由")
    call("POST", f"/keys/requests/{rid2}/claim", ticket=ticket2, expect=409)
    print("  ✓ rejected 后 claim → 409")

    print("[10] 撤回 pending → 不可再审批")
    rid3, ticket3 = submit(tenant="t3", label="撤回样本")
    r = call("DELETE", f"/keys/requests/{rid3}", ticket=ticket3, expect=200)
    must(r.json()["status"] == "cancelled", "申请人撤回成功")
    call("POST", f"/admin/key-requests/{rid3}/approve", admin=True, body={}, expect=409)
    print("  ✓ cancelled 后 approve → 409")

    print(f"\nALL PASS — key requests smoke (owner={OWNER})")


if __name__ == "__main__":
    main()
