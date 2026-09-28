"""mini-envd: in-container data plane service (port 49983).

Implements the E2B envd surface used by the official SDK:
  - GET  /health
  - GET/POST /files                (REST file read/write)
  - POST /process.Process/{Start,List,SendInput,SendSignal,Connect,Update,StreamInput,CloseStdin}
  - POST /filesystem.Filesystem/{MakeDir,Remove,Move,Stat,ListDir}
ConnectRPC: supports both JSON and protobuf codecs (SDK uses JSON).
Server-stream requests use envelopes (1 flag byte + 4-byte BE length).
"""
import asyncio
import json
import os
import shutil
import signal as signal_mod
import subprocess
import time
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from google.protobuf import json_format

from proto import process_pb2, filesystem_pb2

HOME = Path("/home/user")
ENVD_TOKEN = os.getenv("ENVD_TOKEN", "")

app = FastAPI(docs_url=None, redoc_url=None)

PROCS: dict[int, dict] = {}


def check_token(request: Request) -> bool:
    if not ENVD_TOKEN:
        return True
    return request.headers.get("x-access-token") == ENVD_TOKEN


def deny() -> JSONResponse:
    return JSONResponse({"message": "unauthorized"}, status_code=401)


def resolve(path: str) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = HOME / p
    return p


# ---------------- codec helpers ----------------

def parse_envelopes(data: bytes):
    out = []
    i = 0
    while i + 5 <= len(data):
        flags = data[i]
        length = int.from_bytes(data[i + 1:i + 5], "big")
        out.append((flags, data[i + 5:i + 5 + length]))
        i += 5 + length
    return out


def envelope(flags: int, data: bytes) -> bytes:
    return bytes([flags]) + len(data).to_bytes(4, "big") + data


def end_envelope(error: dict | None = None) -> bytes:
    payload = {"error": error} if error else {"metadata": {}}
    return envelope(2, json.dumps(payload).encode())


def connect_error(status: str, message: str, http_status: int = 500):
    return JSONResponse({"code": status, "message": message}, status_code=http_status)


def codec_of(ctype: str) -> str:
    return "json" if "json" in ctype else "proto"


def is_stream_ctype(ctype: str) -> bool:
    return "connect+" in ctype


def decode_req(data: bytes, codec: str, req_cls):
    msg = req_cls()
    if codec == "json":
        json_format.Parse(data, msg, ignore_unknown_fields=True)
    else:
        msg.ParseFromString(data)
    return msg


def encode_msg(msg, codec: str) -> bytes:
    if codec == "json":
        return json_format.MessageToJson(msg).encode()
    return msg.SerializeToString()


async def read_rpc_request(request: Request, req_cls):
    """Returns (codec, message). Handles both unary and enveloped stream bodies."""
    ctype = request.headers.get("content-type", "")
    codec = codec_of(ctype)
    body = await request.body()
    if is_stream_ctype(ctype):
        frames = parse_envelopes(body)
        payload = frames[0][1] if frames else b""
    else:
        payload = body
    return codec, decode_req(payload, codec, req_cls)


def unary_response(msg, codec: str) -> Response:
    if codec == "json":
        return Response(content=encode_msg(msg, codec), media_type="application/json")
    return Response(content=encode_msg(msg, codec), media_type="application/proto")


# ---------------- health ----------------

@app.get("/health")
def health():
    return {"ok": True}


# ---------------- /files REST ----------------

@app.get("/files")
def read_file(request: Request, path: str):
    if not check_token(request):
        return deny()
    fp = resolve(path)
    if not fp.exists():
        return JSONResponse({"message": f"no such file or directory: {path}"}, status_code=404)
    if fp.is_dir():
        return JSONResponse({"message": f"path is a directory: {path}"}, status_code=400)
    return Response(content=fp.read_bytes(), media_type="application/octet-stream")


def _entry_dict(fp: Path, metadata: dict | None = None) -> dict:
    return {
        "name": fp.name,
        "type": "dir" if fp.is_dir() else "file",
        "path": str(fp),
        **({"metadata": metadata} if metadata else {}),
    }


def _save_metadata(fp: Path, metadata: dict):
    store_path = HOME / ".e2b-metadata.json"
    try:
        data = json.loads(store_path.read_text()) if store_path.exists() else {}
        data[str(fp)] = metadata
        store_path.write_text(json.dumps(data))
    except Exception:
        pass


@app.post("/files")
async def write_files(request: Request, path: str | None = None, username: str | None = None):
    if not check_token(request):
        return deny()
    content_type = request.headers.get("content-type", "")
    metadata = {
        k[len("X-Metadata-"):]: v
        for k, v in request.headers.items()
        if k.lower().startswith("x-metadata-")
    }
    results: list[dict] = []

    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        for key, value in form.multi_items():
            if key != "file":
                continue
            file_path = value.filename or path
            if not file_path:
                return JSONResponse({"message": "path required"}, status_code=400)
            fp = resolve(file_path)
            fp.parent.mkdir(parents=True, exist_ok=True)
            data = await value.read()
            fp.write_bytes(data)
            if metadata:
                _save_metadata(fp, metadata)
            results.append(_entry_dict(fp, metadata or None))
    else:
        if not path:
            return JSONResponse({"message": "path required"}, status_code=400)
        body = await request.body()
        if request.headers.get("content-encoding", "").lower() == "gzip":
            import gzip
            body = gzip.decompress(body)
        fp = resolve(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_bytes(body)
        if metadata:
            _save_metadata(fp, metadata)
        results.append(_entry_dict(fp, metadata or None))

    return JSONResponse(results, status_code=200)


# ---------------- process.Process ----------------

@app.post("/process.Process/Start")
async def process_start(request: Request):
    if not check_token(request):
        return deny()
    ctype = request.headers.get("content-type", "")
    codec = codec_of(ctype)
    body = await request.body()
    if is_stream_ctype(ctype):
        frames = parse_envelopes(body)
        payload = frames[0][1] if frames else b""
    else:
        payload = body
    req = decode_req(payload, codec, process_pb2.StartRequest)

    cfg = req.process
    env = dict(os.environ)
    env.update(dict(cfg.envs))
    cwd = str(resolve(cfg.cwd)) if cfg.cwd else str(HOME)
    Path(cwd).mkdir(parents=True, exist_ok=True)

    timeout_ms = None
    ct = request.headers.get("connect-timeout-ms")
    if ct and ct.isdigit():
        timeout_ms = int(ct)

    try:
        proc = subprocess.Popen(
            [cfg.cmd, *cfg.args],
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE if req.stdin else subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError as e:
        err = {"code": "not_found", "message": f"command not found: {cfg.cmd}: {e}"}
        return StreamingResponse([end_envelope(err)], media_type=ctype)

    pid = proc.pid
    PROCS[pid] = {"proc": proc, "tag": req.tag or None, "started": time.time()}

    def start_event_bytes() -> bytes:
        ev = process_pb2.StartResponse(
            event=process_pb2.ProcessEvent(start=process_pb2.ProcessEvent.StartEvent(pid=pid))
        )
        return envelope(0, encode_msg(ev, codec))

    def data_event_bytes(data: bytes, is_stderr: bool) -> bytes:
        ev = process_pb2.ProcessEvent(
            data=process_pb2.ProcessEvent.DataEvent(**({"stderr": data} if is_stderr else {"stdout": data}))
        )
        return envelope(0, encode_msg(process_pb2.StartResponse(event=ev), codec))

    def end_event_bytes(exit_code: int, status: str) -> bytes:
        ev = process_pb2.ProcessEvent(
            end=process_pb2.ProcessEvent.EndEvent(exit_code=exit_code, exited=True, status=status)
        )
        return envelope(0, encode_msg(process_pb2.StartResponse(event=ev), codec))

    def keepalive_bytes() -> bytes:
        ev = process_pb2.StartResponse(
            event=process_pb2.ProcessEvent(keepalive=process_pb2.ProcessEvent.KeepAlive())
        )
        return envelope(0, encode_msg(ev, codec))

    import threading
    q: asyncio.Queue = asyncio.Queue()

    def reader_thread(stream, is_stderr):
        try:
            for line in iter(stream.readline, b""):
                q.put_nowait(("data", line, is_stderr))
        except Exception:
            pass
        q.put_nowait(("eof", b"", is_stderr))

    threading.Thread(target=reader_thread, args=(proc.stdout, False), daemon=True).start()
    threading.Thread(target=reader_thread, args=(proc.stderr, True), daemon=True).start()

    async def stream():
        deadline = time.time() + (timeout_ms / 1000 if timeout_ms else 0)
        killed_by_timeout = False
        eof_count = 0

        yield start_event_bytes()

        while True:
            remaining = deadline - time.time() if deadline else None
            if remaining is not None and remaining <= 0:
                killed_by_timeout = True
                try:
                    proc.kill()
                except Exception:
                    pass
            try:
                kind, data, is_stderr = await asyncio.wait_for(q.get(), timeout=0.5 if deadline else None)
            except asyncio.TimeoutError:
                yield keepalive_bytes()
                continue

            if kind == "data":
                yield data_event_bytes(data, is_stderr)
            elif kind == "eof":
                eof_count += 1
                if eof_count < 2:
                    continue
                rc = proc.poll()
                if rc is None:
                    # process still alive after both pipes closed — keep waiting
                    continue
                status = "killed" if killed_by_timeout else ("completed" if rc == 0 else "failed")
                yield end_event_bytes(-9 if killed_by_timeout else rc, status)
                yield end_envelope()
                return

    return StreamingResponse(stream(), media_type=ctype, headers={"connect-protocol-version": "1"})


@app.post("/process.Process/List")
async def process_list(request: Request):
    if not check_token(request):
        return deny()
    codec, _ = await read_rpc_request(request, process_pb2.ListRequest)
    resp = process_pb2.ListResponse()
    for pid, info in list(PROCS.items()):
        if info["proc"].poll() is not None:
            continue
        pi = resp.processes.add()
        pi.pid = pid
        if info["tag"]:
            pi.tag = info["tag"]
        args = info["proc"].args or [""]
        pi.config.cmd = args[0]
        for a in args[1:]:
            pi.config.args.append(a)
    return unary_response(resp, codec)


@app.post("/process.Process/SendSignal")
async def process_send_signal(request: Request):
    if not check_token(request):
        return deny()
    codec, req = await read_rpc_request(request, process_pb2.SendSignalRequest)
    pid = req.process.pid
    info = PROCS.get(pid)
    if info is None:
        return connect_error("not_found", f"process {pid} not found", 404)
    sig = signal_mod.SIGKILL if req.signal == process_pb2.SIGNAL_SIGKILL else signal_mod.SIGTERM
    try:
        info["proc"].send_signal(sig)
    except Exception as e:
        return connect_error("internal", str(e), 500)
    return unary_response(process_pb2.SendSignalResponse(), codec)


@app.post("/process.Process/SendInput")
async def process_send_input(request: Request):
    if not check_token(request):
        return deny()
    codec, req = await read_rpc_request(request, process_pb2.SendInputRequest)
    info = PROCS.get(req.process.pid)
    if info is None:
        return connect_error("not_found", "process not found", 404)
    proc = info["proc"]
    if proc.stdin and proc.stdin.writable():
        try:
            proc.stdin.write(req.input.stdin)
            proc.stdin.flush()
        except Exception:
            pass
    return unary_response(process_pb2.SendInputResponse(), codec)


@app.post("/process.Process/CloseStdin")
async def process_close_stdin(request: Request):
    if not check_token(request):
        return deny()
    codec, req = await read_rpc_request(request, process_pb2.CloseStdinRequest)
    info = PROCS.get(req.process.pid)
    if info and info["proc"].stdin:
        try:
            info["proc"].stdin.close()
        except Exception:
            pass
    return unary_response(process_pb2.CloseStdinResponse(), codec)


@app.post("/process.Process/Connect")
async def process_connect_rpc(request: Request):
    return connect_error("unimplemented", "connect to running process not supported yet", 501)


@app.post("/process.Process/StreamInput")
async def process_stream_input(request: Request):
    return connect_error("unimplemented", "StreamInput not supported yet", 501)


@app.post("/process.Process/Update")
async def process_update(request: Request):
    codec, _ = await read_rpc_request(request, process_pb2.UpdateRequest)
    return unary_response(process_pb2.UpdateResponse(), codec)


# ---------------- filesystem.Filesystem ----------------

def _entry_info(fp: Path) -> filesystem_pb2.EntryInfo:
    import stat
    from datetime import datetime, timezone
    st = fp.stat()
    e = filesystem_pb2.EntryInfo()
    e.name = fp.name
    e.type = filesystem_pb2.FILE_TYPE_DIRECTORY if fp.is_dir() else filesystem_pb2.FILE_TYPE_FILE
    e.path = str(fp)
    e.size = st.st_size
    e.mode = st.st_mode
    e.permissions = stat.filemode(st.st_mode)[1:]
    try:
        import pwd, grp
        e.owner = pwd.getpwuid(st.st_uid).pw_name
        e.group = grp.getgrgid(st.st_gid).gr_name
    except Exception:
        pass
    e.modified_time.FromDatetime(datetime.fromtimestamp(st.st_mtime, tz=timezone.utc))
    return e


async def _fs_handler(request: Request, req_cls, resp_cls, handler):
    if not check_token(request):
        return deny()
    codec, req = await read_rpc_request(request, req_cls)
    try:
        return handler(req, resp_cls, codec)
    except FileNotFoundError:
        return connect_error("not_found", "no such file or directory", 404)
    except Exception as e:
        return connect_error("internal", str(e), 500)


@app.post("/filesystem.Filesystem/MakeDir")
async def fs_mkdir(request: Request):
    def h(req, resp_cls, codec):
        fp = resolve(req.path)
        fp.mkdir(parents=True, exist_ok=True)
        resp = resp_cls()
        resp.entry.CopyFrom(_entry_info(fp))
        return unary_response(resp, codec)
    return await _fs_handler(request, filesystem_pb2.MakeDirRequest, filesystem_pb2.MakeDirResponse, h)


@app.post("/filesystem.Filesystem/Remove")
async def fs_remove(request: Request):
    def h(req, resp_cls, codec):
        fp = resolve(req.path)
        if not fp.exists():
            return connect_error("not_found", f"no such file or directory: {req.path}", 404)
        if fp.is_dir():
            shutil.rmtree(fp)
        else:
            fp.unlink()
        return unary_response(resp_cls(), codec)
    return await _fs_handler(request, filesystem_pb2.RemoveRequest, filesystem_pb2.RemoveResponse, h)


@app.post("/filesystem.Filesystem/Move")
async def fs_move(request: Request):
    def h(req, resp_cls, codec):
        src = resolve(req.source)
        dst = resolve(req.destination)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        resp = resp_cls()
        resp.entry.CopyFrom(_entry_info(dst))
        return unary_response(resp, codec)
    return await _fs_handler(request, filesystem_pb2.MoveRequest, filesystem_pb2.MoveResponse, h)


@app.post("/filesystem.Filesystem/Stat")
async def fs_stat(request: Request):
    def h(req, resp_cls, codec):
        fp = resolve(req.path)
        if not fp.exists():
            return connect_error("not_found", f"no such file or directory: {req.path}", 404)
        resp = resp_cls()
        resp.entry.CopyFrom(_entry_info(fp))
        return unary_response(resp, codec)
    return await _fs_handler(request, filesystem_pb2.StatRequest, filesystem_pb2.StatResponse, h)


@app.post("/filesystem.Filesystem/ListDir")
async def fs_list_dir(request: Request):
    def h(req, resp_cls, codec):
        fp = resolve(req.path)
        if not fp.is_dir():
            return connect_error("not_found", f"not a directory: {req.path}", 404)
        depth = req.depth or 1
        entries = [fp]
        current = [fp]
        for _ in range(depth - 1):
            nxt = []
            for d in current:
                if d.is_dir():
                    for child in sorted(d.iterdir()):
                        entries.append(child)
                        if child.is_dir():
                            nxt.append(child)
            current = nxt
        resp = resp_cls()
        for e in entries:
            resp.entries.append(_entry_info(e))
        return unary_response(resp, codec)
    return await _fs_handler(request, filesystem_pb2.ListDirRequest, filesystem_pb2.ListDirResponse, h)


@app.post("/filesystem.Filesystem/{rpc}")
async def fs_unimplemented(rpc: str):
    return connect_error("unimplemented", f"rpc {rpc} not supported", 501)
