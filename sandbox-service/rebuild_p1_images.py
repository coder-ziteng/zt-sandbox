"""Rebuild all sandbox images (base + flavours) after envdsvc changes, then clean strays."""
import paramiko, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from deploy_server import build_tar, HOST, USER, PWD, REMOTE_DIR

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(HOST, username=USER, password=PWD, timeout=10)

def run(cmd, timeout=1800, check=True):
    _, stdout, stderr = cli.exec_command(cmd, timeout=timeout)
    rc = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", "replace")
    errout = stderr.read().decode("utf-8", "replace")
    print(f"$ {cmd}\n[rc={rc}]")
    if out.strip():
        print(out[-1500:])
    if errout.strip():
        print("STDERR:", errout[-1000:])
    if check and rc != 0:
        raise SystemExit(f"failed: {cmd}")
    return out

with paramiko.SFTPClient.from_transport(cli.get_transport()) as sftp:
    with sftp.file(f"{REMOTE_DIR}/upload.tar.gz", "wb") as f:
        f.write(build_tar())
print("uploaded")

run(f"cd /srv && tar xzf sandbox-service/upload.tar.gz")
run(f"cd {REMOTE_DIR} && docker build -t sandbox/base:v1 -f images/base/Dockerfile .", timeout=1800)
run(f"cd {REMOTE_DIR} && docker build -t sandbox/browser:v1 -f images/browser/Dockerfile .", timeout=600)
run(f"cd {REMOTE_DIR} && docker build -t sandbox/all-in-one:v1 -f images/all-in-one/Dockerfile .", timeout=600)
run(f"cd {REMOTE_DIR} && docker build -t sandbox/code-interpreter:v1 -f images/code-interpreter/Dockerfile .", timeout=600)
run("docker ps -a --format '{{.Names}}' | grep -E '^sbx-' | xargs -r docker rm -f")
# drop stale metadata (port allocations from removed containers), then re-register templates
run("docker exec sandbox-control-plane sh -c 'rm -f /data/sandbox.db /data/sandbox.db-wal /data/sandbox.db-shm'", check=False)
run("docker restart sandbox-control-plane sandbox-edge-proxy", timeout=120, check=False)
import json, time
time.sleep(6)
from deploy_server import API_KEYS
specs = [
    {"name": "code-interpreter", "image": "sandbox/code-interpreter:v1", "cpuCount": 1, "memoryMB": 2048},
    {"name": "browser", "image": "sandbox/browser:v1", "cpuCount": 1, "memoryMB": 2048, "browserEnabled": True},
    {"name": "all-in-one", "image": "sandbox/all-in-one:v1", "cpuCount": 2, "memoryMB": 3072, "browserEnabled": True},
]
for s in specs:
    run(f"curl -s -X POST http://127.0.0.1:8902/v3/templates -H 'Authorization: Bearer {API_KEYS}' "
        f"-H 'Content-Type: application/json' -d {json.dumps(json.dumps(s))}", check=False)
run(f"curl -s http://127.0.0.1:8902/v2/templates -H 'Authorization: Bearer {API_KEYS}'", check=False)
cli.close()
print("IMAGES REBUILT")
