import paramiko, json

HOST, USER, PWD = "<internal-host>", "root", "123456"

CMDS = [
    ("os", "cat /etc/os-release | head -5"),
    ("kernel", "uname -r"),
    ("arch", "uname -m"),
    ("cpu", "nproc"),
    ("mem", "free -h | head -2"),
    ("disk", "df -h / /home 2>/dev/null | head -5"),
    ("docker", "docker --version 2>&1"),
    ("compose", "docker compose version 2>&1 || docker-compose --version 2>&1"),
    ("py3", "python3 --version 2>&1"),
    ("criu", "which criu 2>&1 || echo no-criu"),
    ("ports", "ss -tlnp 2>/dev/null | awk '{print $4}' | grep -E ':(8902|2000[0-9]|20000|21000)$' || echo ports-free"),
    ("docker-ps", "docker ps -a --format '{{.Names}} {{.Image}} {{.Status}}' 2>&1 | head -10"),
    ("net", "curl -s -o /dev/null -w '%{http_code}' --connect-timeout 5 https://registry-1.docker.io/v2/ 2>&1 || echo no-internet"),
]

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(HOST, username=USER, password=PWD, timeout=10)
out = {}
for k, c in CMDS:
    _, stdout, stderr = cli.exec_command(c, timeout=20)
    body = stdout.read().decode().strip()
    err = stderr.read().decode().strip()
    out[k] = body if body else ("ERR: " + err if err else "(empty)")
cli.close()
print(json.dumps(out, ensure_ascii=False, indent=1))
