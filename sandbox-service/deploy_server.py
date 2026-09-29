"""Deploy sandbox-service to the Linux server: upload, certs, build, compose up, register template."""
import io
import json
import os
import sys
import tarfile
import time

import paramiko


def _require(name):
    val = os.environ.get(name)
    if not val:
        sys.exit(f"[ERROR] env var {name!r} is required (see .env.example)")
    return val


HOST = _require("SBX_SSH_HOST")
USER = _require("SBX_SSH_USER")
PWD = _require("SBX_SSH_PASSWORD")
LOCAL_ROOT = os.path.dirname(os.path.abspath(__file__))
REMOTE_DIR = "/srv/sandbox-service"
DOMAIN = os.environ.get("SBX_DOMAIN", f"{HOST}.nip.io")
API_KEYS = _require("SBX_API_KEYS")
# Optional second key for the official e2b SDK (their client validates the key prefix).
E2B_KEY = os.environ.get("SBX_E2B_KEY", "")
# Optional identity binding for multi-tenant isolation (P4).
API_KEYS_JSON = os.environ.get("SBX_API_KEYS_JSON", "")

SKIP_DIRS = {".venv", "__pycache__", "data", "certs"}
SKIP_FILES = {".gitignore"}

# Write API_KEYS (+ optional e2b key + optional identity JSON) into .env
api_keys_csv = API_KEYS + (f",{E2B_KEY}" if E2B_KEY else "")
env_content = f"API_KEYS={api_keys_csv}\nSANDBOX_DOMAIN={DOMAIN}\n"
if API_KEYS_JSON:
    env_content += f"API_KEYS_JSON={API_KEYS_JSON}\n"
EXTRA = {
    "deploy/.env": env_content,
}


def build_tar() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for root, dirs, files in os.walk(LOCAL_ROOT):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for f in files:
                if f in SKIP_FILES or f.startswith("_"):
                    continue
                full = os.path.join(root, f)
                rel = os.path.relpath(full, LOCAL_ROOT).replace("\\", "/")
                tar.add(full, arcname=f"sandbox-service/{rel}")
        for rel, content in EXTRA.items():
            data = content.encode()
            ti = tarfile.TarInfo(f"sandbox-service/{rel}")
            ti.size = len(data)
            ti.mtime = int(time.time())
            tar.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def run(cli, cmd, timeout=600, check=True):
    _, stdout, stderr = cli.exec_command(cmd, timeout=timeout)
    rc = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", "replace")
    errout = stderr.read().decode("utf-8", "replace")
    print(f"\n$ {cmd}\n[rc={rc}]")
    if out.strip():
        print(out[-4000:])
    if errout.strip():
        print("STDERR:", errout[-2000:])
    if check and rc != 0:
        raise SystemExit(f"command failed: {cmd}")
    return out


def main():
    tdata = build_tar()
    print(f"tar size: {len(tdata)/1024/1024:.1f} MB")

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(HOST, username=USER, password=PWD, timeout=10)

    run(cli, f"mkdir -p {REMOTE_DIR}")
    sftp = cli.open_sftp()
    with sftp.file(f"{REMOTE_DIR}/upload.tar.gz", "wb") as f:
        f.write(tdata)
    print("uploaded")
    run(cli, f"cd /srv && tar xzf sandbox-service/upload.tar.gz && ls sandbox-service")

    # certs
    run(cli, "dnf install -y openssl >/dev/null 2>&1 || yum install -y openssl >/dev/null 2>&1 || true", timeout=300, check=False)
    run(cli, f"cd {REMOTE_DIR} && mkdir -p certs data && if [ ! -f certs/sandbox.crt ]; then "
             f"openssl req -x509 -newkey rsa:2048 -keyout certs/ca.key -out certs/ca.pem -days 3650 -nodes -subj '/CN=sandbox-dev-ca' && "
             f"openssl req -newkey rsa:2048 -keyout certs/sandbox.key -out certs/sandbox.csr -nodes -subj '/CN=*.{DOMAIN}' && "
             f"printf 'subjectAltName=DNS:*.{DOMAIN},DNS:{DOMAIN}\\nbasicConstraints=CA:FALSE\\n' > certs/san.ext && "
             f"openssl x509 -req -in certs/sandbox.csr -CA certs/ca.pem -CAkey certs/ca.key -CAcreateserial -out certs/sandbox.crt -days 3650 -extfile certs/san.ext; fi", check=False)

    # P2 prerequisites: docker experimental (CRIU) + criu binary + checkpoint dir
    run(cli, "mkdir -p /var/lib/sbx-checkpoints", check=False)
    run(cli, "grep -q experimental /etc/docker/daemon.json 2>/dev/null || echo '{\"experimental\": true}' > /etc/docker/daemon.json", check=False)
    run(cli, "dnf install -y criu >/dev/null 2>&1 || true", timeout=300, check=False)
    run(cli, "systemctl restart docker", timeout=180, check=False)
    time.sleep(6)

    # P1/P0 images: shared base (python + envd services + chromium), then the flavours
    run(cli, f"cd {REMOTE_DIR} && docker build -t sandbox/base:v1 -f images/base/Dockerfile .", timeout=1800)
    run(cli, f"cd {REMOTE_DIR} && docker build -t sandbox/browser:v1 -f images/browser/Dockerfile .", timeout=900)
    run(cli, f"cd {REMOTE_DIR} && docker build -t sandbox/all-in-one:v1 -f images/all-in-one/Dockerfile .", timeout=900)
    run(cli, f"cd {REMOTE_DIR} && docker build -t sandbox/code-interpreter:v1 -f images/code-interpreter/Dockerfile .", timeout=900)

    # control-plane image (FastAPI + docker SDK + iptables for the P2 network allowlist)
    run(cli, f"cd {REMOTE_DIR} && docker build -t sandbox/control-plane:v1 -f server/Dockerfile .", timeout=900)

    # compose up
    run(cli, f"cd {REMOTE_DIR}/deploy && docker compose up -d --build", timeout=600)

    time.sleep(3)
    run(cli, "curl -s http://127.0.0.1:8902/health", check=False)

    # register templates (idempotent by name)
    import json as _json
    existing = run(cli, f"curl -s http://127.0.0.1:8902/v2/templates -H 'Authorization: Bearer {API_KEYS}'", check=False)
    try:
        names = {t.get("name") for t in _json.loads(existing.strip() or "[]")}
    except Exception:
        names = set()

    specs = [
        {"name": "code-interpreter", "image": "sandbox/code-interpreter:v1", "cpuCount": 1, "memoryMB": 2048},
        {"name": "browser", "image": "sandbox/browser:v1", "cpuCount": 1, "memoryMB": 2048, "browserEnabled": True},
        {"name": "all-in-one", "image": "sandbox/all-in-one:v1", "cpuCount": 2, "memoryMB": 3072, "browserEnabled": True},
    ]
    for s in specs:
        if s["name"] in names:
            continue
        run(cli, f"curl -s -X POST http://127.0.0.1:8902/v3/templates -H 'Authorization: Bearer {API_KEYS}' "
                 f"-H 'Content-Type: application/json' -d {_json.dumps(_json.dumps(s))}", check=False)
    run(cli, f"curl -s http://127.0.0.1:8902/v2/templates -H 'Authorization: Bearer {API_KEYS}'", check=False)

    # fetch CA cert for client machines (SSL_CERT_FILE)
    os.makedirs(os.path.join(LOCAL_ROOT, "certs"), exist_ok=True)
    with sftp.file(f"{REMOTE_DIR}/certs/ca.pem", "rb") as rf:
        with open(os.path.join(LOCAL_ROOT, "certs", "ca.pem"), "wb") as wf:
            wf.write(rf.read())
    print("ca.pem downloaded to local certs/")

    sftp.close()
    cli.close()
    print("\nDEPLOY DONE")


if __name__ == "__main__":
    main()
