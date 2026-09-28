"""Quick redeploy: only sync code + compose up (skip image build)."""
import paramiko, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from deploy_server import build_tar, HOST, USER, PWD, REMOTE_DIR, API_KEYS

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(HOST, username=USER, password=PWD, timeout=10)

def run(cmd, timeout=300, check=True):
    _, stdout, stderr = cli.exec_command(cmd, timeout=timeout)
    rc = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", "replace")
    errout = stderr.read().decode("utf-8", "replace")
    print(f"$ {cmd}\n[rc={rc}]")
    if out.strip():
        print(out[-3000:])
    if errout.strip():
        print("STDERR:", errout[-1500:])
    if check and rc != 0:
        raise SystemExit(f"failed: {cmd}")
    return out

with paramiko.SFTPClient.from_transport(cli.get_transport()) as sftp:
    with sftp.file(f"{REMOTE_DIR}/upload.tar.gz", "wb") as f:
        f.write(build_tar())
print("uploaded")

run(f"cd /srv && tar xzf sandbox-service/upload.tar.gz")
run(f"cd {REMOTE_DIR}/deploy && docker compose up -d --force-recreate 2>&1 | tail -5", timeout=120, check=False)
import time; time.sleep(3)
run("docker ps --format '{{.Names}} {{.Status}}' | grep sandbox", check=False)
run("curl -s http://127.0.0.1:8902/health && echo", check=False)
run(f"curl -s http://127.0.0.1:8902/v2/templates -H 'Authorization: Bearer {API_KEYS}'", check=False)
cli.close()
print("REDEPLOY DONE")
