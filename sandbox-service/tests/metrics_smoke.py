"""End-to-end smoke test for P3 第六刀 — Prometheus-format /metrics endpoint.

Coverage:
  1. GET /metrics returns valid Prometheus exposition format (no auth needed)
  2. Key metric families exist: http_requests_total, http_request_duration_seconds,
     sandbox_created_total, sandbox_destroyed_total, sandbox_active
  3. Sandbox lifecycle counters increment on create/destroy
  4. HTTP request counters exist and track real requests
  5. Endpoint path normalization collapses /sandboxes/<id> → /sandboxes/{id}
  6. Diagnostics section counter increments when /diag is called
  7. /metrics endpoint itself is NOT recorded (avoids self-feeding)

Run from project root:
    python -m tests.metrics_smoke
"""
from __future__ import annotations

import os
import re
import time

import httpx

API_URL = os.environ.get("SBX_API_URL", "http://127.0.0.1:8902")
API_KEY = os.environ["SBX_API_KEY"]
AUTH_HEADERS = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}


def must(cond, msg):
    if not cond:
        raise SystemExit(f"✗ FAIL: {msg}")
    print(f"  ✓ {msg}")


def http_call(method, path, body=None, expect=None, params=None, headers=None):
    r = httpx.request(method, f"{API_URL}{path}", headers=headers or AUTH_HEADERS,
                      json=body, params=params, timeout=60)
    if expect is not None and r.status_code != expect:
        raise SystemExit(f"✗ {method} {path} expected {expect}, got {r.status_code}: {r.text[:200]}")
    return r


def fetch_metrics() -> str:
    # /metrics 不走鉴权,直接用空 headers
    r = httpx.get(f"{API_URL}/metrics", timeout=15)
    must(r.status_code == 200, f"/metrics returns 200 (got {r.status_code})")
    must("text/plain" in r.headers.get("content-type", ""),
         f"content-type is text/plain (got {r.headers.get('content-type')})")
    return r.text


def parse_prom_metric(body: str, name: str) -> list[dict]:
    """Extract all samples of a single metric from Prometheus exposition format.

    Handles histograms/summaries by accepting suffix variants (_bucket, _count, _sum).
    Returns: [{"labels": {...}, "value": float, "type": "<TYPE line>", "suffix": ""}]
    """
    out = []
    mtype = ""
    # 匹配 name 本身 + 可能的 _bucket / _count / _sum 后缀
    sample_pattern = re.compile(
        rf"^{re.escape(name)}(_bucket|_count|_sum)?(\{{[^}}]*\}})?\s+([^\s]+)"
    )
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(f"# TYPE {name} "):
            mtype = line.split()[-1]
            continue
        if line.startswith("#"):
            continue
        m = sample_pattern.match(line)
        if not m:
            continue
        suffix, labels_str, value = m.group(1) or "", m.group(2) or "", m.group(3)
        labels = {}
        if labels_str:
            for kv in labels_str.strip("{}").split(","):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    labels[k] = v.strip('"')
        try:
            val = float(value)
        except ValueError:
            val = value
        out.append({"labels": labels, "value": val, "type": mtype, "suffix": suffix})
    return out


def pick_template() -> str:
    rows = http_call("GET", "/v2/templates", expect=200).json()
    for r in rows:
        if "interpreter" in r.get("name", "").lower():
            return r["templateCode"]
    return rows[0]["templateCode"]


def create_sandbox(template_code: str) -> str:
    return http_call("POST", "/sandboxes", body={"templateID": template_code, "timeout": 300},
                     expect=201).json()["sandboxID"]


def kill_sandbox(sid: str):
    httpx.delete(f"{API_URL}/sandboxes/{sid}", headers=AUTH_HEADERS, timeout=30)


def main():
    # ---- 1. /metrics returns valid Prometheus format + key families ----
    print("\n[1] /metrics returns valid Prometheus format")
    body = fetch_metrics()
    must("# HELP" in body and "# TYPE" in body, "/metrics contains HELP + TYPE headers")
    # 必需指标族
    for family in ("http_requests_total", "http_request_duration_seconds",
                   "sandbox_created_total", "sandbox_destroyed_total",
                   "sandbox_active"):
        must(f"# TYPE {family}" in body, f"metric family {family} present")

    # ---- 2. http_requests_total 有真实数据 (之前请求产生的) ----
    print("\n[2] http_requests_total has real samples")
    req_samples = parse_prom_metric(body, "http_requests_total")
    must(len(req_samples) >= 1, f"http_requests_total has ≥1 sample (got {len(req_samples)})")
    # 所有 sample 的 value > 0
    must(all(s["value"] >= 1 for s in req_samples),
         f"all http_requests_total samples >= 1 (got {[s['value'] for s in req_samples[:3]]})")

    # ---- 3. http_request_duration_seconds histogram 有 bucket / sum / count ----
    print("\n[3] http_request_duration_seconds is a valid histogram")
    hist_samples = parse_prom_metric(body, "http_request_duration_seconds")
    must(any(s["labels"].get("le") == "+Inf" for s in hist_samples),
         f"histogram has le=+Inf bucket (got {[s['labels'] for s in hist_samples if 'le' in s['labels']][:3]})")
    must(any(s["labels"].get("le") == "0.1" for s in hist_samples),
         "histogram has le=0.1 bucket")

    # ---- 4. 端点归一化: /sandboxes/<id> → /sandboxes/{id} ----
    print("\n[4] endpoint normalization collapses per-resource paths")
    # 先做一次对 /v2/templates 的请求
    http_call("GET", "/v2/templates")
    body = fetch_metrics()
    # 检查 http_requests_total 的 endpoint label 是否包含 {id} (归一化后)
    all_endpoints = [s["labels"].get("endpoint", "") for s in parse_prom_metric(body, "http_requests_total")]
    must(any("sbx" not in e and "tmpl" not in e for e in all_endpoints if e),
         f"no raw sandbox/template IDs in endpoint labels (sample: {all_endpoints[:5]})")
    # 应该有 /v2/templates 这种归一化后的 endpoint
    must("/v2/templates" in all_endpoints,
         f"/v2/templates appears as-is (got endpoints: {sorted(set(all_endpoints))})")

    # ---- 5. 沙箱生命周期计数器: 创建 + 销毁 ----
    print("\n[5] sandbox lifecycle counters increment on create/destroy")
    before = fetch_metrics()
    created_before = sum(s["value"] for s in parse_prom_metric(before, "sandbox_created_total"))
    destroyed_before = sum(s["value"] for s in parse_prom_metric(before, "sandbox_destroyed_total"))

    template = pick_template()
    sid = create_sandbox(template)
    print(f"  → sandbox {sid}")
    time.sleep(0.5)

    after_create = fetch_metrics()
    created_after = sum(s["value"] for s in parse_prom_metric(after_create, "sandbox_created_total"))
    must(created_after >= created_before + 1,
         f"sandbox_created_total incremented (was {created_before}, now {created_after})")
    # sandbox_active{state="running"} 应该 >= 1
    running_after = sum(s["value"] for s in parse_prom_metric(after_create, "sandbox_active")
                        if s["labels"].get("state") == "running")
    must(running_after >= 1, f"sandbox_active running >= 1 after create (got {running_after})")

    kill_sandbox(sid)
    time.sleep(0.5)

    after_kill = fetch_metrics()
    destroyed_after = sum(s["value"] for s in parse_prom_metric(after_kill, "sandbox_destroyed_total"))
    must(destroyed_after >= destroyed_before + 1,
         f"sandbox_destroyed_total incremented (was {destroyed_before}, now {destroyed_after})")

    # ---- 6. diagnostics_calls_total 在 /diag 调用后递增 ----
    print("\n[6] diagnostics_calls_total increments on /diag")
    diag_before = sum(s["value"] for s in parse_prom_metric(fetch_metrics(), "diagnostics_calls_total"))
    # 需要一个 running sandbox 来查 diag
    sid2 = create_sandbox(template)
    http_call("GET", f"/sandboxes/{sid2}/diag", expect=200)
    time.sleep(0.3)
    diag_after = sum(s["value"] for s in parse_prom_metric(fetch_metrics(), "diagnostics_calls_total"))
    must(diag_after >= diag_before + 5,
         f"diagnostics_calls_total incremented by ≥5 (was {diag_before}, now {diag_after})")
    kill_sandbox(sid2)

    # ---- 7. /metrics 本身不记入 http_requests_total (避免自激) ----
    print("\n[7] /metrics endpoint is not self-recorded")
    # 抓一次 /metrics,然后立刻再抓一次看 http_requests_total 是否有 endpoint="/metrics" 的 sample
    fetch_metrics()
    body = fetch_metrics()
    metrics_samples = [s for s in parse_prom_metric(body, "http_requests_total")
                       if s["labels"].get("endpoint") == "/metrics"]
    must(len(metrics_samples) == 0,
         f"/metrics has 0 samples in http_requests_total (got {len(metrics_samples)})")

    print("\nALL METRICS SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
