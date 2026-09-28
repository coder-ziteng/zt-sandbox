"""Rebuild the control-plane image (adds iptables) and restart the stack."""
import paramiko, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from deploy_server import build_tar, HOST, USER, PWD, REMOTE_DIR, API_KEYS

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(HOST, username=USER, password=PWD, timeout=10)

def run(cmd, timeout=600, check=True):
    _, stdout, stderr = cli.exec_command(cmd, timeout=timeout)
    rc = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", "replace")
    errout = stderr.read().decode("utf-8", "replace")
    print(f"$ {cmd}\n[rc={rc}]")
    if out.strip():
        print(out[-2500:])
    if errout.strip():
        print("STDERR:", errout[-1200:])
    if check and rc != 0:
        raise SystemExit(f"failed: {cmd}")
    return out

with paramiko.SFTPClient.from_transport(cli.get_transport()) as sftp:
    with sftp.file(f"{REMOTE_DIR}/upload.tar.gz", "wb") as f:
        f.write(build_tar())
print("uploaded")

run(f"cd /srv && tar xzf sandbox-service/upload.tar.gz")
run(f"cd {REMOTE_DIR} && docker build -t sandbox/control-plane:v1 -f server/Dockerfile .", timeout=900)
run(f"cd {REMOTE_DIR}/deploy && docker compose up -d", timeout=300)
time.sleep(6)
run("curl -s http://127.0.0.1:8902/health")
run("docker exec sandbox-control-plane iptables --version", check=False)
run("docker exec sandbox-control-plane iptables -t filter -L DOCKER-USER -n | head -3", check=False)
cli.close()
print("CONTROL PLANE REBUILT")
