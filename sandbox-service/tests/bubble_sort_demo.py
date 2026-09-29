"""Bubble sort demo: create sandbox via REST, run code through jupyter /execute, kill.

Usage: python tests/bubble_sort_demo.py <api-key> [--tunnel]
  --tunnel  route edge-proxy 443 through an SSH tunnel (for hosts that block 443)
"""
import json
import os
import socket
import sys
import threading
import time

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_FILE = os.path.join(os.path.dirname(ROOT), ".env")
if os.path.exists(ENV_FILE):
    with open(ENV_FILE, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, _, v = ln.partition("=")
                os.environ.setdefault(k.strip(), v.strip())
os.environ["SSL_CERT_FILE"] = os.path.join(ROOT, "certs", "ca.pem")
for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(var, None)
os.environ["NO_PROXY"] = os.environ.get("SBX_NO_PROXY", "192.168.2.162,192.168.2.162.nip.io")

API = os.environ.get("SBX_API_URL", "http://192.168.2.162:8902")
DOMAIN = os.environ.get("SBX_DOMAIN", "192.168.2.162.nip.io")
KEY = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("SBX_E2B_KEY", "")
HEADERS = {"X-API-KEY": KEY, "Authorization": f"Bearer {KEY}"}

CODE = '''
import random

def bubble_sort(arr):
    a = arr[:]
    n = len(a)
    for i in range(n - 1):
        swapped = False
        for j in range(n - 1 - i):
            if a[j] > a[j + 1]:
                a[j], a[j + 1] = a[j + 1], a[j]
                swapped = True
        if not swapped:
            break
    return a

data = [random.randint(0, 100) for _ in range(15)]
print("before:", data)
result = bubble_sort(data)
print("after: ", result)
assert result == sorted(data)
print("bubble sort OK")
result
'''


def _pump(a, b):
    try:
        while True:
            data = a.recv(65536)
            if not data:
                break
            b.sendall(data)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def ssh_tunnel():
    """Forward a random local port to 127.0.0.1:443 on the sandbox host via SSH."""
    import paramiko

    host = os.environ.get("SBX_SSH_HOST", API.split("//")[1].split(":")[0])
    user = os.environ.get("SBX_SSH_USER", "root")
    pwd = os.environ.get("SBX_SSH_PASSWORD", "")
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(host, username=user, password=pwd, timeout=15)
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    port = srv.getsockname()[1]

    def accept_loop():
        while True:
            try:
                s, _ = srv.accept()
            except OSError:
                return
            ch = cli.get_transport().open_channel("direct-tcpip", ("127.0.0.1", 443), s.getsockname())
            threading.Thread(target=_pump, args=(s, ch), daemon=True).start()
            threading.Thread(target=_pump, args=(ch, s), daemon=True).start()

    threading.Thread(target=accept_loop, daemon=True).start()
    print(f"ssh tunnel: 127.0.0.1:{port} -> {host}:443")
    return cli, port


def main():
    use_tunnel = "--tunnel" in sys.argv
    r = httpx.get(f"{API}/v2/templates", headers=HEADERS, timeout=10)
    r.raise_for_status()
    tid = next(t["templateID"] for t in r.json() if t["name"] == "code-interpreter")

    r = httpx.post(f"{API}/sandboxes", headers=HEADERS, timeout=90,
                   json={"templateID": tid, "timeout": 600})
    r.raise_for_status()
    info = r.json()
    sid = info["sandboxID"]
    token = info.get("envdAccessToken", "")
    print(f"sandbox created: {sid}")

    for _ in range(30):
        h = httpx.get(f"{API}/sandboxes/{sid}/health", headers=HEADERS, timeout=10).json()
        if all(h.get(k, {}).get("ok") for k in ("envd", "jupyter")):
            break
        time.sleep(2)
    else:
        sys.exit("sandbox not healthy in time")
    print("sandbox healthy")

    host_header = f"49999-{sid}.{DOMAIN}"
    ssh = None
    if use_tunnel:
        ssh, tport = ssh_tunnel()
        url = f"https://127.0.0.1:{tport}/execute"
        headers = {"Host": host_header, "X-Access-Token": token, "Content-Type": "application/json"}
        verify = False
    else:
        url = f"https://{host_header}/execute"
        headers = {"X-Access-Token": token, "Content-Type": "application/json"}
        verify = os.path.join(ROOT, "certs", "ca.pem")

    with httpx.stream("POST", url, timeout=120, verify=verify, headers=headers,
                      json={"code": CODE, "context_id": "default"}) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line.strip():
                continue
            ev = json.loads(line)
            t = ev.get("type")
            if t in ("stdout", "stderr"):
                print(f"[{t}] {ev['text'].rstrip()}")
            elif t == "result":
                print(f"[result] {ev}")
            elif t == "error":
                print(f"[error] {ev.get('name')}: {ev.get('value')}")
            elif t == "number_of_executions":
                print(f"[exec_count] {ev['execution_count']}")

    r = httpx.delete(f"{API}/sandboxes/{sid}", headers=HEADERS, timeout=30)
    print(f"sandbox killed: HTTP {r.status_code}")
    if ssh:
        ssh.close()


if __name__ == "__main__":
    main()
