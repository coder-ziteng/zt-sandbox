"""Grab browser_svc tracebacks from the newest sandbox container."""
import paramiko

CMD = r"""
n=$(docker ps -a --format '{{.Names}}' | grep -E '^sbx-' | head -1)
echo "container=$n"
docker logs "$n" 2>&1 | grep -nE 'Traceback|Error|Exception|500' | tail -10
echo "---- context ----"
docker logs "$n" 2>&1 | grep -A 25 'Traceback' | tail -40
"""

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(HOST, username=USER, password=PWD, timeout=15)
_, o, e = cli.exec_command(CMD, timeout=120)
out = o.read().decode("utf-8", "replace")
cli.close()
print(out)
