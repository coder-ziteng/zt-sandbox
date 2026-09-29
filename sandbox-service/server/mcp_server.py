"""MCP Server for zt-Sandbox.

Exposes sandbox lifecycle + compute + filesystem operations as MCP tools, so
Claude Code / Cursor can drive the sandbox service without a custom client library.

Transport: stdio (per Anthropic MCP spec). The server runs as a separate process
spawned by the MCP host (Claude Code, Cursor, etc.).

Architecture:
  MCP server ──HTTP──▶  control plane :8902  (lifecycle, metadata)
              ──HTTPS─▶  edge-proxy :443    (subdomain → data plane)
                            │
                            ▼
                     {containerPort}-{sandboxID}.{DOMAIN}
                     ↳ 49983 → envd :49983  (files, process)
                     ↳ 49999 → jupyter :49999 (code execution)
                     ↳ 3000  → browser :3000  (CDP, screenshots)

Configuration (env vars):
  SBX_API_URL     — control plane base URL, e.g. http://192.168.2.162:8902
  SBX_API_KEY     — control plane API key (Bearer)
  SBX_DOMAIN      — public sandbox domain for subdomain routing, e.g. 192.168.2.162.nip.io
  SBX_CA_CERT     — path to CA cert (for self-signed edge-proxy TLS)
                    if unset and SBX_INSECURE=1, falls back to verify=False
  SBX_INSECURE    — "1" disables TLS verification (dev only)
  SBX_HTTP_TIMEOUT — per-call timeout in seconds (default 120)

Tool surface (12 tools):
  list_templates, create_sandbox, list_sandboxes, get_sandbox,
  kill_sandbox, pause_sandbox, resume_sandbox,
  run_code, run_command,
  files_read, files_write, files_list

Reference: https://modelcontextprotocol.io/specification
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import ssl
import sys
import time
from typing import Any

import httpx

# MCP SDK — stdio server. Install with `pip install mcp>=1.0`
try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import TextContent, Tool
except ImportError:
    sys.stderr.write(
        "ERROR: `mcp` package required. Install with: pip install 'mcp>=1.0'\n"
    )
    raise

# Reuse compiled proto module for process.Process/Start (ConnectRPC protobuf).
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROTO_PATH = os.path.normpath(os.path.join(_HERE, "..", "envdsvc"))
if _PROTO_PATH not in sys.path:
    sys.path.insert(0, _PROTO_PATH)
try:
    import proto.process_pb2 as process_pb2  # type: ignore[no-redef]  # noqa: E402
except ImportError:
    process_pb2 = None  # run_command will report unavailability


# ---------------- config ----------------

API_URL = os.environ.get("SBX_API_URL", "http://127.0.0.1:8902").rstrip("/")
API_KEY = os.environ.get("SBX_API_KEY", "")
DOMAIN = os.environ.get("SBX_DOMAIN", "").strip()
CA_CERT = os.environ.get("SBX_CA_CERT", "").strip()
INSECURE = os.environ.get("SBX_INSECURE", "") == "1"
HTTP_TIMEOUT = float(os.environ.get("SBX_HTTP_TIMEOUT", "120"))

if not API_KEY:
    sys.stderr.write("ERROR: SBX_API_KEY env var is required\n")
    sys.exit(2)
if not DOMAIN:
    sys.stderr.write(
        "ERROR: SBX_DOMAIN env var is required (e.g. 192.168.2.162.nip.io)\n"
        "       This is the public sandbox domain used for subdomain routing.\n"
    )
    sys.exit(2)


def _build_ssl_context() -> ssl.SSLContext | bool:
    """Resolve TLS verification: CA cert → insecure flag → certifi default."""
    if CA_CERT and os.path.exists(CA_CERT):
        return ssl.create_default_context(cafile=CA_CERT)
    if INSECURE:
        sys.stderr.write(
            "WARNING: SBX_INSECURE=1 set — TLS verification disabled, "
            "vulnerable to MITM. Use only in dev.\n"
        )
        return False
    if CA_CERT:
        sys.stderr.write(
            f"WARNING: SBX_CA_CERT={CA_CERT} not found; falling back to insecure.\n"
        )
        return False
    # No CA cert configured — use certifi defaults (will fail against self-signed CA).
    return True


SSL_VERIFY = _build_ssl_context()

CTRL_HEADERS = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}

# Shared HTTP clients (created at import time, closed in main()).
_ctrl_http = httpx.AsyncClient(timeout=HTTP_TIMEOUT)
_data_http = httpx.AsyncClient(timeout=HTTP_TIMEOUT, verify=SSL_VERIFY)


# ---------------- helpers ----------------

class McpError(Exception):
    """Raised by handlers to surface a clean error message to MCP."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _err(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        return f"[{resp.status_code}] {body.get('message') or body.get('code') or resp.text}"
    except Exception:
        return f"[{resp.status_code}] {resp.text[:300]}"


async def _ctrl(method: str, path: str, **kw) -> Any:
    """Hit the control plane (REST, plaintext HTTP)."""
    r = await _ctrl_http.request(method, f"{API_URL}{path}", headers=CTRL_HEADERS, **kw)
    if r.status_code >= 400:
        raise McpError(_err(r))
    if r.status_code == 204 or not r.content:
        return None
    return r.json()


def _data_url(container_port: int, sandbox_id: str, path: str) -> str:
    """Build data plane URL via edge-proxy subdomain routing."""
    return f"https://{container_port}-{sandbox_id}.{DOMAIN}{path}"


async def _data(meta: dict, container_port: int, method: str, path: str,
                headers: dict | None = None, **kw) -> httpx.Response:
    """Hit the data plane via edge-proxy subdomain routing."""
    h = dict(headers or {})
    h.setdefault("X-Access-Token", meta["envd_token"])
    url = _data_url(container_port, meta["sandbox_id"], path)
    return await _data_http.request(method, url, headers=h, **kw)


# ---------------- sandbox info cache ----------------
# sandbox_id -> { envd_token, domain, state, ... }
_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 30.0


async def _sandbox_meta(sandbox_id: str) -> dict:
    now = time.time()
    cached = _CACHE.get(sandbox_id)
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1]
    row = await _ctrl("GET", f"/sandboxes/{sandbox_id}")
    info = {
        "sandbox_id": sandbox_id,
        "envd_token": row.get("envdAccessToken") or row.get("envd_token"),
        "state": row.get("state"),
        "template_id": row.get("templateID"),
        "pause_mode": row.get("pauseMode"),
        "domain": row.get("domain") or DOMAIN,
    }
    if not info["envd_token"]:
        raise McpError(
            "sandbox row missing envdAccessToken — likely paused; call resume_sandbox first"
        )
    _CACHE[sandbox_id] = (now, info)
    return info


def _drop_cache(sandbox_id: str) -> None:
    _CACHE.pop(sandbox_id, None)


# Shared httpx client for data plane calls (HTTPS via subdomain).
_data_http = httpx.AsyncClient(timeout=HTTP_TIMEOUT, verify=SSL_VERIFY)


# ---------------- tool definitions ----------------

TOOLS: list[dict] = [
    {
        "name": "list_templates",
        "description": "列出所有可用的沙箱模板（含代码执行、浏览器、All-in-One 三类）",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "create_sandbox",
        "description": "从模板创建一个新沙箱实例，返回 sandbox_id（用于后续 run_code / run_command / files_* 调用）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "template_id": {"type": "string", "description": "模板代码（如 tmpl_xxx）"},
                "timeout": {"type": "integer", "default": 600, "description": "存活秒数，到期自动回收"},
                "metadata": {"type": "object", "description": "附加元数据，键值对"},
            },
            "required": ["template_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_sandboxes",
        "description": "列出当前活跃的沙箱（running + paused 状态）",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_sandbox",
        "description": "查询单个沙箱的当前状态、过期时间等",
        "inputSchema": {
            "type": "object",
            "properties": {"sandbox_id": {"type": "string"}},
            "required": ["sandbox_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "kill_sandbox",
        "description": "强制销毁沙箱（容器 + 元数据），不可恢复",
        "inputSchema": {
            "type": "object",
            "properties": {"sandbox_id": {"type": "string"}},
            "required": ["sandbox_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "pause_sandbox",
        "description": "暂停沙箱（默认尝试 CRIU 保留内存；不可用时降级为 docker stop，仅保留文件系统）",
        "inputSchema": {
            "type": "object",
            "properties": {"sandbox_id": {"type": "string"}},
            "required": ["sandbox_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "resume_sandbox",
        "description": "恢复已暂停的沙箱；恢复成功后 sandbox_id 不变，可继续 run_code 等",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sandbox_id": {"type": "string"},
                "timeout": {"type": "integer", "default": 300},
            },
            "required": ["sandbox_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "run_code",
        "description": "在沙箱的 Jupyter 内核中执行 Python 代码；返回 stdout/stderr 及执行序号。多次调用共享同一个 kernel 变量",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sandbox_id": {"type": "string"},
                "code": {"type": "string", "description": "要执行的 Python 代码"},
            },
            "required": ["sandbox_id", "code"],
            "additionalProperties": False,
        },
    },
    {
        "name": "run_command",
        "description": "在沙箱内执行 shell 命令（前台），返回 stdout/stderr/exit_code",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sandbox_id": {"type": "string"},
                "command": {"type": "string", "description": "要执行的 shell 命令字符串"},
                "cwd": {"type": "string", "description": "工作目录，可选"},
            },
            "required": ["sandbox_id", "command"],
            "additionalProperties": False,
        },
    },
    {
        "name": "files_read",
        "description": "读取沙箱内文件内容（UTF-8 文本或 base64 二进制）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sandbox_id": {"type": "string"},
                "path": {"type": "string", "description": "容器内绝对路径"},
            },
            "required": ["sandbox_id", "path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "files_write",
        "description": "写入文件到沙箱（覆盖已有内容）。二进制请先 base64 编码并设 is_binary=true",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sandbox_id": {"type": "string"},
                "path": {"type": "string"},
                "content": {"type": "string", "description": "文本内容或 base64 编码后的二进制"},
                "is_binary": {"type": "boolean", "default": False},
            },
            "required": ["sandbox_id", "path", "content"],
            "additionalProperties": False,
        },
    },
    {
        "name": "files_list",
        "description": "列出沙箱内目录下的条目（文件名 + 类型）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sandbox_id": {"type": "string"},
                "path": {"type": "string", "description": "目录路径，默认 /home/user"},
            },
            "required": ["sandbox_id"],
            "additionalProperties": False,
        },
    },
]


# Container ports (fixed; mapped by edge-proxy subdomain → host_port)
ENVD_PORT = 49983
JUPYTER_PORT = 49999


# ---------------- tool handlers ----------------

async def _list_templates(_args: dict) -> list[TextContent]:
    rows = await _ctrl("GET", "/v2/templates")
    summary = "\n".join(
        f"- {t.get('templateCode')} ({t.get('name')}, browser={t.get('browserEnabled')}, "
        f"net={t.get('networkPolicy', {}).get('mode')})"
        for t in rows
    ) or "(no templates registered)"
    return [TextContent(type="text", text=summary)]


async def _create_sandbox(args: dict) -> list[TextContent]:
    template_id = args["template_id"]
    timeout = int(args.get("timeout") or 600)
    metadata = args.get("metadata") or {}
    body = {"templateID": template_id, "timeout": timeout, "metadata": metadata}
    row = await _ctrl("POST", "/sandboxes", json=body)
    sid = row["sandboxID"]
    return [TextContent(
        type="text",
        text=f"sandbox_id={sid}\nstate={row.get('state')}\n"
             f"template={template_id}\ntimeout={timeout}s\n"
             f"features={row.get('features')}\n"
             f"endAt={row.get('endAt')}",
    )]


async def _list_sandboxes(_args: dict) -> list[TextContent]:
    rows = await _ctrl("GET", "/v2/sandboxes")
    if not rows:
        return [TextContent(type="text", text="(no active sandboxes)")]
    lines = [
        f"- {r['sandboxID']} state={r['state']} tpl={r.get('templateID')} "
        f"end={r.get('endAt')}" for r in rows
    ]
    return [TextContent(type="text", text="\n".join(lines))]


async def _get_sandbox(args: dict) -> list[TextContent]:
    sid = args["sandbox_id"]
    row = await _ctrl("GET", f"/sandboxes/{sid}")
    _drop_cache(sid)
    return [TextContent(type="text", text=json.dumps(row, ensure_ascii=False, indent=2))]


async def _kill_sandbox(args: dict) -> list[TextContent]:
    sid = args["sandbox_id"]
    await _ctrl("DELETE", f"/sandboxes/{sid}")
    _drop_cache(sid)
    return [TextContent(type="text", text=f"killed {sid}")]


async def _pause_sandbox(args: dict) -> list[TextContent]:
    sid = args["sandbox_id"]
    await _ctrl("POST", f"/sandboxes/{sid}/pause", json={"criu": "auto"})
    _drop_cache(sid)
    row = await _ctrl("GET", f"/sandboxes/{sid}")
    return [TextContent(type="text", text=f"paused {sid} (pauseMode={row.get('pauseMode')})")]


async def _resume_sandbox(args: dict) -> list[TextContent]:
    sid = args["sandbox_id"]
    timeout = int(args.get("timeout") or 300)
    await _ctrl("POST", f"/sandboxes/{sid}/resume", json={"timeout": timeout})
    _drop_cache(sid)
    return [TextContent(type="text", text=f"resumed {sid} (timeout={timeout}s)")]


async def _run_code(args: dict) -> list[TextContent]:
    sid = args["sandbox_id"]
    code = args["code"]
    meta = await _sandbox_meta(sid)
    r = await _data(meta, JUPYTER_PORT, "POST", "/execute",
                    headers={"Content-Type": "application/json"},
                    content=json.dumps({"code": code}).encode())
    if r.status_code == 404:
        raise McpError("sandbox has no Jupyter kernel (browser-only template?)")
    if r.status_code >= 400:
        raise McpError(f"jupyter /execute -> {_err(r)}")
    # NDJSON stream: stdout/stderr/result/error/number_of_executions
    out_lines: list[str] = []
    for raw in r.text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            continue
        kind = ev.get("type") or ev.get("kind")
        if kind in ("stdout", "stderr"):
            out_lines.append(ev.get("text", ""))
        elif kind == "result":
            data = ev.get("data", "")
            mime = ev.get("mime", "text/plain")
            out_lines.append(f"[result {mime}]\n{data}")
        elif kind == "error":
            tb = "\n".join(ev.get("traceback", []))
            out_lines.append(f"[error] {ev.get('ename','')}: {ev.get('evalue','')}\n{tb}")
        elif kind == "number_of_executions":
            out_lines.append(f"[exec_count={ev.get('value')}]")
    return [TextContent(type="text", text="\n".join(out_lines) or "(no output)")]


async def _run_command(args: dict) -> list[TextContent]:
    sid = args["sandbox_id"]
    command = args["command"]
    cwd = args.get("cwd") or "/home/user"
    meta = await _sandbox_meta(sid)
    if process_pb2 is None:
        raise McpError("proto.process_pb2 not importable; run_command unavailable")

    start_req = process_pb2.StartRequest()
    start_req.process.cmd = "/bin/sh"
    start_req.process.args.append(command)
    start_req.process.cwd = cwd
    payload = start_req.SerializeToString()
    envelope = b"\x00" + len(payload).to_bytes(4, "big") + payload

    r = await _data(meta, ENVD_PORT, "POST", "/process.Process/Start",
                    headers={"Content-Type": "application/connect+proto",
                             "Connect-Protocol-Version": "1"},
                    content=envelope)
    if r.status_code >= 400:
        raise McpError(f"process.Process/Start -> {_err(r)}")
    # Response is ConnectRPC streaming — parse each envelope.
    events: list[Any] = []
    pos = 0
    body = r.content
    while pos + 5 <= len(body):
        flag = body[pos]
        ln = int.from_bytes(body[pos + 1:pos + 5], "big")
        chunk = body[pos + 5:pos + 5 + ln]
        pos += 5 + ln
        if flag & 0x01:
            try:
                msg = json.loads(chunk.decode())
                events.append(("trailer", msg))
            except Exception:
                events.append(("trailer", chunk.decode("utf-8", "replace")))
            break
        try:
            resp = process_pb2.StartResponse()
            resp.ParseFromString(chunk)
            events.append({"event": "start", "pid": resp.pid})
        except Exception:
            events.append({"event": "raw", "data": chunk.decode("utf-8", "replace")})

    return [TextContent(type="text", text=json.dumps(events, ensure_ascii=False, indent=2))]


async def _files_read(args: dict) -> list[TextContent]:
    sid = args["sandbox_id"]
    path = args["path"]
    meta = await _sandbox_meta(sid)
    r = await _data(meta, ENVD_PORT, "GET", f"/files?path={path}")
    if r.status_code == 404:
        raise McpError(f"file not found: {path}")
    if r.status_code >= 400:
        raise McpError(_err(r))
    try:
        text = r.content.decode("utf-8")
        return [TextContent(type="text", text=text)]
    except UnicodeDecodeError:
        b64 = base64.b64encode(r.content).decode()
        return [TextContent(
            type="text",
            text=f"[binary, {len(r.content)} bytes, base64]\n{b64}",
        )]


async def _files_write(args: dict) -> list[TextContent]:
    sid = args["sandbox_id"]
    path = args["path"]
    content = args["content"]
    is_binary = bool(args.get("is_binary"))
    meta = await _sandbox_meta(sid)
    body = base64.b64decode(content) if is_binary else content.encode("utf-8")
    r = await _data(meta, ENVD_PORT, "POST", f"/files?path={path}",
                    headers={"Content-Type": "application/octet-stream"},
                    content=body)
    if r.status_code >= 400:
        raise McpError(_err(r))
    return [TextContent(type="text", text=f"wrote {len(body)} bytes to {path}")]


async def _files_list(args: dict) -> list[TextContent]:
    sid = args["sandbox_id"]
    path = args.get("path") or "/home/user"
    meta = await _sandbox_meta(sid)
    # Filesystem.ListDir — ConnectRPC JSON codec
    r = await _data(meta, ENVD_PORT, "POST", "/filesystem.Filesystem/ListDir",
                    headers={"Content-Type": "application/json",
                             "Connect-Protocol-Version": "1"},
                    content=json.dumps({"path": path}).encode())
    if r.status_code >= 400:
        raise McpError(_err(r))
    try:
        body = json.loads(r.content)
        entries = body.get("entries", [])
    except Exception:
        entries = []
    lines = [f"{e.get('type','?'):4} {e.get('name','?')}" for e in entries] or ["(empty)"]
    return [TextContent(type="text", text="\n".join(lines))]


HANDLERS = {
    "list_templates": _list_templates,
    "create_sandbox": _create_sandbox,
    "list_sandboxes": _list_sandboxes,
    "get_sandbox": _get_sandbox,
    "kill_sandbox": _kill_sandbox,
    "pause_sandbox": _pause_sandbox,
    "resume_sandbox": _resume_sandbox,
    "run_code": _run_code,
    "run_command": _run_command,
    "files_read": _files_read,
    "files_write": _files_write,
    "files_list": _files_list,
}


# ---------------- MCP server bootstrap ----------------

server = Server("zt-sandbox")


@server.list_tools()
async def _list_tools() -> list[Tool]:
    return [Tool(
        name=t["name"],
        description=t["description"],
        inputSchema=t["inputSchema"],
    ) for t in TOOLS]


@server.call_tool()
async def _call_tool(name: str, arguments: dict) -> list[TextContent]:
    handler = HANDLERS.get(name)
    if not handler:
        raise McpError(f"unknown tool: {name}")
    try:
        return await handler(arguments or {})
    except McpError:
        raise
    except httpx.HTTPError as e:
        raise McpError(f"transport error: {e}")


# Shared client for control-plane calls (plain HTTP, low overhead)
_ctrl_client = httpx.AsyncClient(timeout=HTTP_TIMEOUT)


async def main():
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    finally:
        await _data_http.aclose()
        await _ctrl_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())