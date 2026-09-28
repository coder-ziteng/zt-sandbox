"""P2 probe #2: checkpoint round-trip using --checkpoint-dir (bypasses containerd content store)."""
import paramiko

CMD = r"""
set -x
mkdir -p /var/lib/sbx-checkpoints
docker rm -f criu-probe2 2>/dev/null
docker run -d --name criu-probe2 --network=sandbox-net m.daocloud.io/docker.io/library/python:3.11-slim sh -c 'i=0; while true; do echo tick$i; i=$((i+1)); sleep 1; done'
sleep 2
docker logs criu-probe2 2>&1 | head -3
docker checkpoint create --checkpoint-dir=/var/lib/sbx-checkpoints criu-probe2 cp1 2>&1 | tail -3
docker ps -a --format '{{.Names}} {{.Status}}' | grep criu-probe2
ls /var/lib/sbx-checkpoints/cp1 2>&1 | head -5
sleep 2
docker start --checkpoint-dir=/var/lib/sbx-checkpoints --checkpoint cp1 criu-probe2 2>&1 | tail -3
sleep 3
docker ps --format '{{.Names}} {{.Status}}' | grep criu-probe2
sleep 2
echo "== logs after restore (should continue numbering) =="
docker logs criu-probe2 2>&1 | tail -5
docker rm -f criu-probe2
"""

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(HOST, username=USER, password=PWD, timeout=15)
_, o, e = cli.exec_command(CMD, timeout=420)
out = o.read().decode("utf-8", "replace")
err = e.read().decode("utf-8", "replace")
cli.close()
print(out)
if err.strip():
    print("STDERR:", err[-2000:])
