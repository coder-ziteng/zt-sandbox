"""TLS edge proxy: routes {containerPort}-{sandboxID}.{domain} -> 127.0.0.1:{hostPort}.

Reads port mappings from the shared SQLite DB (WAL, read-only usage).

P1: supports port 3000 (browser) and transparent WebSocket tunnelling, which is
required for Playwright/Puppeteer CDP (wss://3000-<sandboxID>.<domain>/devtools/...).
"""
import asyncio
import logging
import os
import sqlite3
import ssl

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("edge-proxy")

DB_PATH = "/data/sandbox.db"
CERT_DIR = os.getenv("CERT_DIR", "/certs")
UPSTREAM_HOST = "127.0.0.1"


def resolve_route(sandbox_id: str, container_port: int):
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        column = {49983: "host_port_envd", 49999: "host_port_jupyter", 3000: "host_port_browser"}.get(container_port)
        if column:
            row = conn.execute(
                f"SELECT {column} AS hp FROM sandboxes WHERE sandbox_id=?", (sandbox_id,)
            ).fetchone()
            if row and row[0]:
                return row[0]
        # fallback: allocations in ascending order -> [envd, jupyter, browser]
        rows = conn.execute(
            "SELECT host_port FROM port_allocations WHERE sandbox_id=? ORDER BY host_port", (sandbox_id,)
        ).fetchall()
        ports = [r[0] for r in rows]
        idx = {49983: 0, 49999: 1, 3000: 2}.get(container_port)
        if idx is not None and len(ports) > idx:
            return ports[idx]
    finally:
        conn.close()
    return None


def parse_host(host_header: str):
    host = host_header.split(":")[0]
    labels = host.split(".")
    first = labels[0]
    if "-" not in first:
        return None
    port_str, sandbox_id = first.split("-", 1)
    if not port_str.isdigit() or not sandbox_id.startswith("sbx"):
        return None
    return sandbox_id, int(port_str)


async def pipe(src, dst):
    try:
        while True:
            data = await src.read(65536)
            if not data:
                break
            dst.write(data)
            await dst.drain()
    except Exception:
        pass


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    host_header = ""
    try:
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = await asyncio.wait_for(reader.read(65536), timeout=60)
            if not chunk:
                return
            head += chunk
        head_part, _, rest = head.partition(b"\r\n\r\n")
        request_line = head_part.split(b"\r\n")[0].decode("latin-1")
        headers_raw = head_part.split(b"\r\n")[1:]

        content_length = 0
        chunked = False
        upgrade = False
        for h in headers_raw:
            k, _, v = h.decode("latin-1").partition(":")
            k = k.strip().lower()
            v = v.strip()
            if k == "host":
                host_header = v
            elif k == "content-length":
                content_length = int(v or 0)
            elif k == "transfer-encoding" and "chunked" in v.lower():
                chunked = True
            elif k == "upgrade":
                upgrade = True

        route = parse_host(host_header)
        if route is None:
            body = b'{"code":100004,"message":"invalid sandbox host"}'
            writer.write(
                b"HTTP/1.1 400 Bad Request\r\nContent-Type: application/json\r\nContent-Length: "
                + str(len(body)).encode() + b"\r\n\r\n" + body
            )
            await writer.drain()
            return

        sandbox_id, container_port = route
        host_port = await asyncio.get_event_loop().run_in_executor(
            None, resolve_route, sandbox_id, container_port
        )
        if host_port is None:
            body = b'{"code":100003,"message":"sandbox not found"}'
            writer.write(
                b"HTTP/1.1 404 Not Found\r\nContent-Type: application/json\r\nContent-Length: "
                + str(len(body)).encode() + b"\r\n\r\n" + body
            )
            await writer.drain()
            return

        log.info("route %s -> 127.0.0.1:%s (%s%s)", host_header, host_port, request_line,
                 " [websocket]" if upgrade else "")
        upstream = await asyncio.open_connection(UPSTREAM_HOST, host_port)
        ur, uw = upstream

        new_headers = []
        for h in headers_raw:
            k, _, v = h.decode("latin-1").partition(":")
            kl = k.strip().lower()
            if kl in ("connection", "keep-alive", "proxy-connection", "upgrade"):
                continue
            new_headers.append(h)
        if upgrade:
            conn_hdr = b"Connection: Upgrade\r\nUpgrade: websocket\r\n"
        else:
            conn_hdr = b"Connection: close\r\n"

        payload = (request_line.encode("latin-1") + b"\r\n" + b"\r\n".join(new_headers) + b"\r\n"
                   + conn_hdr + b"\r\n" + rest)
        uw.write(payload)
        await uw.drain()

        t1 = asyncio.create_task(pipe(reader, uw))
        t2 = asyncio.create_task(pipe(ur, writer))
        done, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
    except Exception as e:
        log.warning("proxy error: %s (host=%s)", e, host_header)
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def main():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(f"{CERT_DIR}/sandbox.crt", f"{CERT_DIR}/sandbox.key")
    ctx.set_alpn_protocols(["http/1.1"])
    server = await asyncio.start_server(handle, "0.0.0.0", 443, ssl=ctx)
    log.info("edge proxy listening on :443 (db=%s)", DB_PATH)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
