"""Per-sandbox diagnostic snapshot — point-in-time introspection.

P3 第五刀: 给 Agent / 运维一组调试数据,在一次 round-trip 内拿到沙箱
的全部关键状态。每个 section 独立失败(容器已退出时部分仍可读),
任一 section 报错只在该 section 返回 {"error": ...},不影响其他字段。

Section:
  processes    container.top()      PID / 用户 / CPU 时间 / 命令
  stats        container.stats()    CPU% / 内存 / 网络 IO / 块 IO
  logs         container.logs()     最近 N 行 stdout/stderr(分别捕获)
  connections  exec "ss -tlnp"      容器内的 TCP/UDP 监听与连接
  envd         httpx probe          端口可达性 + 延迟 + 原始 /health 返回

API:
  gather(sandbox_id, include=None, log_tail=100) -> dict

  include: 可选集合,默认包含全部;支持 {"processes","stats","logs",
           "connections","envd"} 的任意子集。
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, cast

import httpx

# 复用 runtime.py 的 Docker client,避免双 socket
from runtime import client

log = logging.getLogger("diagnostics")

ALL_SECTIONS = ("processes", "stats", "logs", "connections", "envd")
DEFAULT_LOG_TAIL = 100
ENV_HTTP_TIMEOUT = 5.0  # envd probe 用,长点免得假阴性


def _container(sandbox_id: str):
    """Get the Docker container or raise docker.errors.NotFound."""
    return client.containers.get(f"sbx-{sandbox_id}")


def _container_meta(c) -> dict:
    """Cheap attrs from container (no Docker stats call)."""
    try:
        a = c.attrs
    except Exception:
        return {"status": c.status, "error": "attrs unavailable"}
    hc = a.get("HostConfig") or {}
    net_mode = hc.get("NetworkMode") or "bridge"
    net = a.get("NetworkSettings", {}).get("Networks", {}) or {}
    ip = ""
    for v in net.values():
        ip = v.get("IPAddress", "") or ""
        if ip:
            break
    # host-network 沙箱没有 bridge IP; 显式置 127.0.0.1 让调试更直观
    if net_mode == "host" and not ip:
        ip = "127.0.0.1"
    return {
        "id": c.id[:12],
        "name": c.name,
        "image": (a.get("Config", {}).get("Image") or ""),
        "status": c.status,
        "created": a.get("Created", ""),
        "networkMode": net_mode,
        "ipAddress": ip,
    }


def processes(sandbox_id: str) -> dict:
    try:
        c = _container(sandbox_id)
    except Exception as e:
        return {"error": f"container not found: {e}"}
    try:
        top = c.top() or {}
    except Exception as e:
        return {"error": f"top failed: {e}"}
    titles = top.get("Titles") or []
    rows = top.get("Processes") or []
    out = []
    for r in rows:
        rec = {}
        for i, t in enumerate(titles):
            rec[t.lower()] = r[i] if i < len(r) else ""
        out.append(rec)
    return {"count": len(out), "list": out}


def _calc_cpu_pct(stats: dict) -> float | None:
    """Compute CPU% from Docker stats JSON (two-sample delta)."""
    cur_cpu = stats.get("cpu_stats", {}).get("cpu_usage", {}).get("total_usage", 0) or 0
    pre_cpu = stats.get("precpu_stats", {}).get("cpu_usage", {}).get("total_usage", 0) or 0
    cur_sys = stats.get("cpu_stats", {}).get("system_cpu_usage", 0) or 0
    pre_sys = stats.get("precpu_stats", {}).get("system_cpu_usage", 0) or 0
    cpu_delta = cur_cpu - pre_cpu
    sys_delta = cur_sys - pre_sys
    if sys_delta <= 0:
        return None
    ncpu = len(stats.get("cpu_stats", {}).get("cpu_usage", {}).get("percpu_usage") or [1])
    return round((cpu_delta / sys_delta) * ncpu * 100.0, 2)


def _sum_networks(stats: dict) -> tuple[int, int]:
    rx = tx = 0
    for v in (stats.get("networks") or {}).values():
        rx += int(v.get("rx_bytes", 0) or 0)
        tx += int(v.get("tx_bytes", 0) or 0)
    return rx, tx


def _block_io(stats: dict) -> tuple[int, int]:
    io = (stats.get("blkio_stats") or {}).get("io_service_bytes_recursive") or []
    r = w = 0
    for entry in io:
        op = (entry.get("op") or "").lower()
        if op == "read":
            r += int(entry.get("value", 0) or 0)
        elif op == "write":
            w += int(entry.get("value", 0) or 0)
    return r, w


def stats(sandbox_id: str) -> dict:
    try:
        c = _container(sandbox_id)
    except Exception as e:
        return {"error": f"container not found: {e}"}
    try:
        # stream=False → single snapshot, prev/current 已包含
        s = cast(dict, c.stats(stream=False))
    except Exception as e:
        return {"error": f"stats failed: {e}"}
    mem = s.get("memory_stats") or {}
    usage = mem.get("usage") or 0
    limit = mem.get("limit") or 0
    rx, tx = _sum_networks(s)
    br, bw = _block_io(s)
    return {
        "cpuPct": _calc_cpu_pct(s),
        "memUsageBytes": usage,
        "memLimitBytes": limit,
        "memPct": round(usage / limit * 100.0, 2) if limit else None,
        "netRxBytes": rx,
        "netTxBytes": tx,
        "blockReadBytes": br,
        "blockWriteBytes": bw,
        "ts": time.time(),
    }


def logs(sandbox_id: str, tail: int = DEFAULT_LOG_TAIL) -> dict:
    try:
        c = _container(sandbox_id)
    except Exception as e:
        return {"error": f"container not found: {e}"}
    try:
        # Docker SDK 的 Container.logs() 不总是支持 demux(早期版本没有)
        # 分两次取 stdout / stderr,再合并为字符串
        out_b = c.logs(stdout=True, stderr=False, tail=tail)
        err_b = c.logs(stdout=False, stderr=True, tail=tail)
    except Exception as e:
        return {"error": f"logs failed: {e}"}
    if isinstance(out_b, (list, bytes)) and isinstance(out_b, list):
        out_b = b"".join(out_b)
    if isinstance(err_b, (list, bytes)) and isinstance(err_b, list):
        err_b = b"".join(err_b)
    return {
        "stdout": (out_b or b"").decode("utf-8", errors="replace") if isinstance(out_b, (bytes, bytearray)) else str(out_b),
        "stderr": (err_b or b"").decode("utf-8", errors="replace") if isinstance(err_b, (bytes, bytearray)) else str(err_b),
        "linesShown": tail,
        "truncated": True,  # Docker tail 不告诉你总共多少,只能假设截断
    }


# /proc/net/tcp 行解析 (hex, little-endian IP on x86):
#   sl  local_address      rem_address        st  ...
#   0:  00000000:4E51      00000000:0000      0A  ...
# st: 0A=LISTEN, 01=ESTABLISHED, 06=TIME_WAIT, 07=CLOSE, 08=CLOSE_WAIT
import re
_TCP_STATES = {
    "01": "ESTABLISHED", "02": "SYN_SENT", "03": "SYN_RECV",
    "04": "FIN_WAIT1", "05": "FIN_WAIT2", "06": "TIME_WAIT",
    "07": "CLOSE", "08": "CLOSE_WAIT", "09": "LAST_ACK",
    "0A": "LISTEN", "0B": "CLOSING",
}


def _hex_ip_port(hex_s: str) -> str:
    """Convert '00000000:4E51' (little-endian) to '0.0.0.0:20001'."""
    if not hex_s or ":" not in hex_s:
        return hex_s
    ip_hex, port_hex = hex_s.split(":", 1)
    try:
        ip_int = int(ip_hex, 16)
        # little-endian (x86) → big-endian bytes
        ip_bytes = ip_int.to_bytes(4, "little")
        ip = ".".join(str(b) for b in ip_bytes)
        port = int(port_hex, 16)
        return f"{ip}:{port}"
    except Exception:
        return hex_s


# ss -tlnp 输出示例:
#   State    Recv-Q  Send-Q   Local Address:Port    Peer Address:Port
#   LISTEN   0       128      0.0.0.0:49983         0.0.0.0:*   users:(("python3",pid=42,fd=7))
_SS_LINE = re.compile(
    r"^(?P<state>\S+)\s+\d+\s+\d+\s+(?P<local>[0-9.:*]+)\s+(?P<remote>[0-9.:*]+)"
    r"(?:\s+users:\(\((?P<proc>[^)]*)\))?"
)
_SS_PROC = re.compile(r'\("(?P<cmd>[^"]+)",pid=(?P<pid>\d+),fd=\d+\)')


def _parse_ss(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("State") or line.startswith("Netid"):
            continue
        m = _SS_LINE.match(line)
        if not m:
            continue
        rec = {
            "state": m.group("state"),
            "local": m.group("local"),
            "remote": m.group("remote"),
            "pid": None,
            "cmd": None,
        }
        proc = m.group("proc")
        if proc:
            pm = _SS_PROC.search(proc)
            if pm:
                rec["cmd"] = pm.group("cmd")
                rec["pid"] = int(pm.group("pid"))
        out.append(rec)
    return out


def _parse_proc_net(text: str, proto: str = "tcp") -> list[dict]:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("sl"):
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        try:
            local = _hex_ip_port(parts[1])
            remote = _hex_ip_port(parts[2])
            st = _TCP_STATES.get(parts[3].upper(), parts[3])
        except Exception:
            continue
        out.append({
            "proto": proto,
            "local": local,
            "remote": remote,
            "state": st,
            "pid": None,
            "cmd": None,
        })
    return out


def connections(sandbox_id: str) -> dict:
    """Get container's TCP listening sockets.

    Strategy:
      1) try `ss -tlnp` (gives cmd + pid)
      2) try `netstat -tlnp`
      3) fallback: read /proc/net/tcp + /proc/net/tcp6 (always present in
         Linux containers; no cmd/pid, but shows ports and states)

    This ensures the section works even on minimal images that ship neither
    ss nor netstat.
    """
    try:
        c = _container(sandbox_id)
    except Exception as e:
        return {"error": f"container not found: {e}"}
    # Try ss/netstat first (richer output)
    try:
        res = c.exec_run(["sh", "-c", "ss -tlnp 2>/dev/null || netstat -tlnp 2>/dev/null"],
                         demux=True, stream=False, detach=False)
        exit_code = res.exit_code
        if exit_code == 0 and res.output and res.output[0]:
            text = res.output[0].decode("utf-8", errors="replace")
            rows = _parse_ss(text)
            if rows:
                return {"count": len(rows), "list": rows, "source": "ss/netstat",
                        "rawLines": text.count("\n")}
    except Exception as e:
        log.debug("ss/netstat exec failed: %s", e)
    # Fallback: read /proc/net/tcp + /proc/net/tcp6
    try:
        out4 = c.exec_run(["cat", "/proc/net/tcp"], demux=True, stream=False, detach=False)
        out6 = c.exec_run(["cat", "/proc/net/tcp6"], demux=True, stream=False, detach=False)
        rows = []
        if out4.exit_code == 0 and out4.output and out4.output[0]:
            rows.extend(_parse_proc_net(out4.output[0].decode("utf-8", errors="replace"), "tcp"))
        if out6.exit_code == 0 and out6.output and out6.output[0]:
            rows.extend(_parse_proc_net(out6.output[0].decode("utf-8", errors="replace"), "tcp6"))
        return {"count": len(rows), "list": rows, "source": "proc/net"}
    except Exception as e:
        return {"error": f"ss/netstat/proc all failed: {e}"}


def envd(sandbox_id: str, host_port_envd: int) -> dict:
    """Hit the sandbox's envd /health directly via the host port mapping."""
    url = f"http://127.0.0.1:{host_port_envd}/health"
    t0 = time.time()
    try:
        r = httpx.get(url, timeout=ENV_HTTP_TIMEOUT)
        dt = (time.time() - t0) * 1000
        try:
            raw = r.json()
        except Exception:
            raw = {"raw": r.text[:200]}
        return {
            "reachable": r.status_code == 200,
            "statusCode": r.status_code,
            "latencyMs": round(dt, 2),
            "raw": raw,
        }
    except httpx.HTTPError as e:
        return {"reachable": False, "error": f"{type(e).__name__}: {e}",
                "latencyMs": round((time.time() - t0) * 1000, 2)}


def gather(sandbox_id: str, include: list[str] | None = None,
           log_tail: int = DEFAULT_LOG_TAIL,
           host_port_envd: int | None = None) -> dict:
    """Return a diagnostic snapshot. `include` defaults to all sections.

    Sections are collected sequentially and independently — a failure in
    one does not stop the others. The endpoint always returns a top-level
    `container` meta block plus whatever sections were requested.
    """
    sections = list(include) if include else list(ALL_SECTIONS)
    bad = [s for s in sections if s not in ALL_SECTIONS]
    if bad:
        return {"error": f"unknown include: {bad}",
                "valid": list(ALL_SECTIONS)}

    try:
        c = _container(sandbox_id)
        meta = _container_meta(c)
    except Exception as e:
        return {"sandboxID": sandbox_id, "error": f"container not found: {e}"}

    out: dict[str, Any] = {
        "sandboxID": sandbox_id,
        "container": meta,
        "ts": time.time(),
    }

    def _envd_runner(sid=sandbox_id) -> dict:
        if not host_port_envd:
            return {"error": "envdPort unknown (sandbox row missing host_port_envd)"}
        return envd(sid, host_port_envd)

    runners = {
        "processes": lambda: processes(sandbox_id),
        "stats": lambda: stats(sandbox_id),
        "logs": lambda: logs(sandbox_id, tail=log_tail),
        "connections": lambda: connections(sandbox_id),
        "envd": _envd_runner,
    }
    for s in sections:
        try:
            out[s] = runners[s]()
        except Exception as e:
            log.warning("diag %s.%s failed: %s", sandbox_id, s, e)
            out[s] = {"error": f"{type(e).__name__}: {e}"}
    return out
