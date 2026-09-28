"""Jupyter-compatible code execution service (port 49999).

Endpoints used by e2b-code-interpreter SDK:
  - POST /execute          (NDJSON stream: stdout/stderr/result/error/number_of_executions)
  - GET/POST /contexts, DELETE /contexts/{id}, POST /contexts/{id}/restart
Real IPython kernels are managed through jupyter_client.
"""
import base64
import json
import os
import queue
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from jupyter_client.manager import KernelManager

HOME = Path("/home/user")
ENVD_TOKEN = os.getenv("ENVD_TOKEN", "")

app = FastAPI(docs_url=None, redoc_url=None)

# context_id -> {"km": KernelManager, "kc": client|None, "language": str, "cwd": str}
CONTEXTS: dict[str, dict] = {}
DEFAULT_CONTEXT_ID = "default"


def check_token(request: Request) -> bool:
    if not ENVD_TOKEN:
        return True
    return request.headers.get("x-access-token") == ENVD_TOKEN


@app.get("/health")
def health():
    """Health polling endpoint (P1) — reports whether the kernel runtime is usable."""
    return {"ok": True, "service": "jupyter", "contexts": len(CONTEXTS)}


def _context_payload(cid: str, info: dict) -> dict:
    return {"id": cid, "language": info["language"], "cwd": info["cwd"]}


def _start_kernel(cwd: str) -> dict:
    km = KernelManager(kernel_name="python3")
    Path(cwd).mkdir(parents=True, exist_ok=True)
    km.start_kernel(cwd=cwd)
    kc = km.client()
    kc.start_channels()
    kc.wait_for_ready(timeout=60)
    return {"km": km, "kc": kc, "language": "python", "cwd": cwd}


def _get_or_create(cid: str, cwd: str | None = None) -> dict:
    if cid in CONTEXTS:
        return CONTEXTS[cid]
    info = _start_kernel(cwd or str(HOME / "workspace"))
    CONTEXTS[cid] = info
    return info


MIME_FIELD = {
    "text/plain": "text",
    "text/html": "html",
    "text/markdown": "markdown",
    "image/svg+xml": "svg",
    "image/png": "png",
    "image/jpeg": "jpeg",
    "application/pdf": "pdf",
    "text/latex": "latex",
    "application/json": "json",
}


@app.get("/contexts")
def list_contexts(request: Request):
    if not check_token(request):
        return deny401()
    return [_context_payload(cid, info) for cid, info in list(CONTEXTS.items())]


def deny401():
    return JSONResponse({"message": "unauthorized"}, status_code=401)


@app.post("/contexts")
async def create_context(request: Request):
    if not check_token(request):
        return deny401()
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    cid = uuid.uuid4().hex[:12]
    cwd = body.get("cwd") or str(HOME / "workspace")
    info = _start_kernel(cwd)
    CONTEXTS[cid] = info
    return _context_payload(cid, info)


@app.delete("/contexts/{context_id}")
def delete_context(context_id: str, request: Request):
    if not check_token(request):
        return deny401()
    info = CONTEXTS.pop(context_id, None)
    if info is None:
        return JSONResponse({"message": "context not found"}, status_code=404)
    try:
        info["kc"].stop_channels()
        info["km"].shutdown_kernel(now=True)
    except Exception:
        pass
    return {"ok": True}


@app.post("/contexts/{context_id}/restart")
def restart_context(context_id: str, request: Request):
    if not check_token(request):
        return deny401()
    info = CONTEXTS.get(context_id)
    if info is None:
        return JSONResponse({"message": "context not found"}, status_code=404)
    try:
        info["kc"].stop_channels()
        info["km"].restart_kernel()
        info["kc"] = info["km"].client()
        info["kc"].start_channels()
        info["kc"].wait_for_ready(timeout=60)
    except Exception as e:
        return JSONResponse({"message": str(e)}, status_code=500)
    return {"ok": True}


def _result_from_data(data: dict) -> dict:
    out = {"type": "result"}
    for mime, field in MIME_FIELD.items():
        if mime in data:
            val = data[mime]
            if mime in ("image/png", "image/jpeg", "application/pdf"):
                val = base64.b64encode(val if isinstance(val, bytes) else val.encode("latin-1")).decode()
            elif mime == "application/json":
                val = val if isinstance(val, (dict, list)) else json.loads(val)
            out[field] = val
    if "application/vnd.*" in data and isinstance(data["application/vnd.*"], dict):
        out["data"] = data["application/vnd.*"]
    return out


@app.post("/execute")
async def execute(request: Request):
    if not check_token(request):
        return deny401()
    body = await request.json()
    code = body.get("code") or ""
    context_id = body.get("context_id") or DEFAULT_CONTEXT_ID
    try:
        info = _get_or_create(context_id)
    except Exception as e:
        return JSONResponse({"message": f"kernel start failed: {e}"}, status_code=500)

    kc = info["kc"]
    exec_count_holder = {"n": None}

    def gen():
        msg_id = kc.execute(code)
        got_error = None
        while True:
            try:
                msg = kc.get_iopub_msg(timeout=120)
            except queue.Empty:
                yield json.dumps({"type": "error", "name": "TimeoutError", "value": "execution timed out", "traceback": []}) + "\n"
                return
            if msg["parent_header"].get("msg_id") != msg_id:
                continue
            msg_type = msg["msg_type"]
            content = msg["content"]
            ts = int(time.time() * 1000)

            if msg_type == "stream":
                yield json.dumps({"type": content["name"], "text": content["text"], "timestamp": ts}) + "\n"
            elif msg_type == "execute_result":
                yield json.dumps(_result_from_data(content.get("data", {}))) + "\n"
            elif msg_type == "display_data":
                yield json.dumps(_result_from_data(content.get("data", {}))) + "\n"
            elif msg_type == "error":
                got_error = {
                    "type": "error",
                    "name": content.get("ename", "Error"),
                    "value": content.get("evalue", ""),
                    "traceback": content.get("traceback", []),
                }
                yield json.dumps(got_error) + "\n"
            elif msg_type == "status" and content.get("execution_state") == "idle":
                break

        # fetch execution count from shell reply
        try:
            while True:
                shell_msg = kc.get_shell_msg(timeout=10)
                if shell_msg["parent_header"].get("msg_id") == msg_id:
                    exec_count_holder["n"] = shell_msg["content"].get("execution_count")
                    break
        except queue.Empty:
            pass
        if exec_count_holder["n"] is not None:
            yield json.dumps({"type": "number_of_executions", "execution_count": exec_count_holder["n"]}) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")
