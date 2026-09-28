"""Probe server capabilities needed by P1 (browser image) and P2 (net policy / CRIU)."""
import json
import paramiko

HOST, USER, PWD = "<internal-host>", "root", "123456"

CMD = r"""
echo "== os"; cat /etc/os-release | head -2
echo "== cpu/mem"; nproc; free -m | head -2; df -h / | tail -1
echo "== iptables"; which iptables iptables-nft 2>/dev/null; iptables --version 2>&1 | head -1
echo "== dockeruser-chain"; iptables -t filter -L DOCKER-USER -n 2>&1 | head -5
echo "== docker"; docker version --format '{{.Server.Version}}'; docker info --format '{{.ExperimentalBuild}}' 2>/dev/null
echo "== daemon.json"; cat /etc/docker/daemon.json 2>/dev/null || echo "(none)"
echo "== criu"; dnf -q list criu 2>&1 | tail -4; which criu || echo "criu-binary-missing"
echo "== images"; docker images --format '{{.Repository}}:{{.Tag}} {{.Size}}' | head -10
echo "== running"; docker ps --format '{{.Names}}' | head -10
echo "== chromium-apt"; docker run --rm m.daocloud.io/docker.io/library/python:3.11-slim bash -c "apt-get update -qq 2>&1 | tail -2; apt-cache policy chromium 2>&1 | head -4" 2>&1 | tail -8
"""

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(HOST, username=USER, password=PWD, timeout=15)
_, o, e = cli.exec_command(CMD, timeout=300)
out = o.read().decode("utf-8", "replace")
err = e.read().decode("utf-8", "replace")
cli.close()

print(out)
if err.strip():
    print("STDERR:", err[-1500:])
