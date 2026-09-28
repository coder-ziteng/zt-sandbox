"""P2 probe: enable docker experimental + install CRIU + actually try a checkpoint round-trip."""
import paramiko

HOST, USER, PWD = "<internal-host>", "root", "123456"

CMD = r"""
set -x
echo '{"experimental": true}' > /etc/docker/daemon.json
systemctl restart docker
sleep 5
docker info --format '{{.ExperimentalBuild}}'
echo "== checkpoint cli =="
docker checkpoint ls nonexistent 2>&1 | head -3
docker container checkpoint --help 2>&1 | head -5
echo "== criu install =="
dnf install -y criu 2>&1 | tail -5
criu --version 2>&1 | head -3
echo "== criu check =="
criu check --all 2>&1 | tail -12
echo "== roundtrip =="
docker rm -f criu-probe 2>/dev/null
docker run -d --name criu-probe --network=none m.daocloud.io/docker.io/library/python:3.11-slim sleep 600
sleep 2
docker checkpoint create criu-probe cp1 2>&1 | tail -5
docker ps -a --format '{{.Names}} {{.Status}}' | grep criu-probe
docker start --checkpoint cp1 criu-probe 2>&1 | tail -5
sleep 2
docker ps --format '{{.Names}} {{.Status}}' | grep criu-probe
docker rm -f criu-probe 2>&1
"""

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(HOST, username=USER, password=PWD, timeout=15)
_, o, e = cli.exec_command(CMD, timeout=600)
out = o.read().decode("utf-8", "replace")
err = e.read().decode("utf-8", "replace")
cli.close()
print(out)
if err.strip():
    print("STDERR:", err[-2000:])
