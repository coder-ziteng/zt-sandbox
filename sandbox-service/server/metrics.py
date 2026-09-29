"""Observability metrics for the control plane.

P3 第六刀: 把关键业务事件和 HTTP 流量暴露成 Prometheus 可抓取的
/metrics 端点。选 prometheus-client 而不是直接上 OTLP 推流,因为:

  1. 自包含 — /metrics 端点天然兼容任何 TSDB(Prometheus / VictoriaMetrics /
     Datadog Agent / OTel Collector 的 prometheus receiver)
  2. 零配置 — 不需要部署 OTLP 推送目标就能拿到数据
  3. 易扩展 — opentelemetry-exporter-prometheus 可直接桥接到 OTLP,
     只需在 OTLP_ENDPOINT env 存在时启用,代码结构不用改

指标一览:
  沙箱生命周期
    sandbox_created_total{template}          创建次数
    sandbox_destroyed_total{reason}          销毁次数 (user / timeout / watchdog)
    sandbox_active{state}                    当前 running / paused 沙箱数

  HTTP
    http_requests_total{method,endpoint,status}  请求次数
    http_request_duration_seconds{method,endpoint} 请求时延

  网络策略
    netpolicy_applied_total                   网络白名单 apply 次数
    netpolicy_refreshed_total                 网络白名单 refresh 次数

  生命周期钩子
    hook_invocations_total{kind}              hook 触发次数 (startup / periodic)
    hook_failures_total{kind}                 hook 失败次数

  诊断 API
    diagnostics_calls_total{section}          diag 端点 section 被访问次数
"""
from __future__ import annotations

import logging

from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST

log = logging.getLogger("metrics")


# ---------------------------------------------------------------------------
# 沙箱生命周期
# ---------------------------------------------------------------------------
SANDBOX_CREATED = Counter(
    "sandbox_created_total",
    "Sandboxes created since process start",
    ["template"],
)
SANDBOX_DESTROYED = Counter(
    "sandbox_destroyed_total",
    "Sandboxes destroyed since process start",
    ["reason"],  # user / timeout / watchdog
)
SANDBOX_ACTIVE = Gauge(
    "sandbox_active",
    "Currently active sandboxes",
    ["state"],  # running / paused
)


# ---------------------------------------------------------------------------
# HTTP 流量 (FastAPI 中间件记录)
# ---------------------------------------------------------------------------
HTTP_REQUESTS = Counter(
    "http_requests_total",
    "HTTP requests handled",
    ["method", "endpoint", "status"],
)
HTTP_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["method", "endpoint"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)


# ---------------------------------------------------------------------------
# 网络策略
# ---------------------------------------------------------------------------
NETPOLICY_APPLIED = Counter(
    "netpolicy_applied_total",
    "Netpolicy rules applied (allowlist / open)",
)
NETPOLICY_REFRESHED = Counter(
    "netpolicy_refreshed_total",
    "Netpolicy rules refreshed (manual or background tick)",
)


# ---------------------------------------------------------------------------
# 生命周期钩子
# ---------------------------------------------------------------------------
HOOK_INVOCATIONS = Counter(
    "hook_invocations_total",
    "Lifecycle hook invocations",
    ["kind"],  # startup / periodic
)
HOOK_FAILURES = Counter(
    "hook_failures_total",
    "Lifecycle hook failures",
    ["kind"],
)


# ---------------------------------------------------------------------------
# 诊断 API
# ---------------------------------------------------------------------------
DIAG_CALLS = Counter(
    "diagnostics_calls_total",
    "Diagnostics endpoint invocations by section",
    ["section"],
)


# ---------------------------------------------------------------------------
# /metrics 端点的辅助函数
# ---------------------------------------------------------------------------
def render() -> bytes:
    """Return the full metrics payload in Prometheus exposition format."""
    return generate_latest()


CONTENT_TYPE = CONTENT_TYPE_LATEST


# ---------------------------------------------------------------------------
# 端点路径归一化 (防止 /sandboxes/{id} 的每个 id 变成单独 metric series)
# ---------------------------------------------------------------------------
def normalize_endpoint(path: str) -> str:
    """Collapse per-resource paths to their pattern form.

    /sandboxes/sbxabc123           → /sandboxes/{id}
    /sandboxes/sbxabc123/pause     → /sandboxes/{id}/pause
    /sandboxes/sbxabc123/diag      → /sandboxes/{id}/diag
    /templates/tmplabc123/hooks    → /templates/{id}/hooks
    /templates/tmplabc123          → /templates/{id}
    /health / /metrics             → as-is
    """
    parts = [p for p in path.split("/") if p]
    # 长度 > 0 的部分中,把以 sbx/tmpl 开头的段视为 {id}
    normalized = []
    for p in parts:
        if p.startswith(("sbx", "tmpl")):
            normalized.append("{id}")
        else:
            normalized.append(p)
    return "/" + "/".join(normalized)
