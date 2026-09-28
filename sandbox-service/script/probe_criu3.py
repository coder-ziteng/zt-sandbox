"""P2 probe #3: determine whether docker checkpoint restore works at all on this daemon."""
import paramiko

CMD = r"""
set -x
docker info --format 'driver={{.Driver}}'
docker info 2>/dev/null | grep -i -m3 'containerd'
docker rm -f criu-p3 2>/dev/null
docker run -d --name criu-p3 --network=sandbox-net m.daocloud.io/docker.io/library/python:3.11-slim sh -c 'i=0; while true; do echo t$i; i=$((i+1)); sleep 1; done'
sleep 2
echo "--- dump cp1"
docker checkpoint create criu-p3 cp1 2>&1 | tail -3
echo "--- list"
docker checkpoint ls criu-p3 2>&1 | tail -3
echo "--- restore attempt 1"
docker start --checkpoint cp1 criu-p3 2>&1 | tail -3
sleep 2
docker ps -a --format '{{.Names}} {{.Status}}' | grep criu-p3
echo "--- restore attempt 2"
docker start --checkpoint cp1 criu-p3 2>&1 | tail -3
sleep 2
docker ps -a --format '{{.Names}} {{.Status}}' | grep criu-p3
echo "--- fresh dump cp2 + restore"
docker rm -f criu-p3 2>/dev/null
docker run -d --name criu-p3b --network=sandbox-net m.daocloud.io/docker.io/library/python:3.11-slim sh -c 'i=0; while true; do echo u$i; i=$((i+1)); sleep 1; done'
sleep 2
docker checkpoint create criu-p3b cp2 2>&1 | tail -2
docker start --checkpoint cp2 criu-p3b 2>&1 | tail -2
sleep 3
docker ps -a --format '{{.Names}} {{.Status}}' | grep criu-p3b
docker logs criu-p3b 2>&1 | tail -4
docker rm -f criu-p3 criu-p3b 2>/dev/null
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
    print("STDERR:", err[-2500:])
