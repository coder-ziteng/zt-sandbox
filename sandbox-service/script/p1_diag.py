"""Diagnose P1 all-in-one sandbox creation failure."""
import paramiko

HOST, USER, PWD = "<internal-host>", "root", "123456"

CMD = r"""
echo "== free"; free -m | head -2
echo "== containers"; docker ps -a --format '{{.Names}} {{.Status}}' | grep -E 'sbx|sandbox' | head -10
echo "== control-plane logs"; docker logs sandbox-control-plane 2>&1 | tail -30
echo "== newest sbx logs"; n=$(docker ps -a --format '{{.Names}}' | grep -E '^sbx-' | head -1); echo "container=$n"; docker logs "$n" 2>&1 | tail -25
echo "== health"; curl -s -m 5 http://127.0.0.1:8902/health; echo
"""

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(HOST, username=USER, password=PWD, timeout=15)
_, o, e = cli.exec_command(CMD, timeout=120)
out = o.read().decode("utf-8", "replace")
cli.close()
print(out)
