"""Why isn't browser_svc running inside the sandbox container?"""
import paramiko

HOST, USER, PWD = "<internal-host>", "root", "123456"

CMD = r"""
n=$(docker ps -a --format '{{.Names}}' | grep -E '^sbx-' | head -1)
echo "container=$n"
echo "== env"; docker exec "$n" env | grep -E 'SBX_FEATURES|ENVD' 
echo "== full logs (grep)"; docker logs "$n" 2>&1 | grep -iE 'traceback|error|browser|3000|ImportError|ModuleNotFound' | head -20
echo "== head of logs"; docker logs "$n" 2>&1 | head -20
echo "== start.sh in image"; docker exec "$n" cat /app/start.sh | head -30
echo "== run browser_svc manually"; docker exec "$n" sh -c 'cd /app && timeout 8 python -c "import browser_svc; print(\"import ok\")"' 2>&1 | tail -8
echo "== chromium"; docker exec "$n" sh -c 'which chromium; chromium --version 2>&1 | head -2'
"""

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(HOST, username=USER, password=PWD, timeout=15)
_, o, e = cli.exec_command(CMD, timeout=120)
out = o.read().decode("utf-8", "replace")
cli.close()
print(out)
