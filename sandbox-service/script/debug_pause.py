import paramiko, sys
cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect("<internal-host>", username="root", password="<redacted>", timeout=10)

def run(cmd, timeout=30):
    _, stdout, _ = cli.exec_command(cmd, timeout=timeout)
    print(f"$ {cmd}\n{stdout.read().decode()}")

run("docker logs sandbox-edge-proxy 2>&1 | tail -8")
run("docker logs sandbox-control-plane 2>&1 | tail -6")
run("docker ps -a --format '{{.Names}} {{.Status}}' | grep -E '^sbx-'")
run("curl -s -m 3 http://127.0.0.1:20014/health && echo <-20014 || echo 20014-down")
run("curl -s -m 3 http://127.0.0.1:20016/health && echo <-20016 || echo 20016-down")
cli.close()
