import paramiko
cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect("<internal-host>", username="root", password="<redacted>", timeout=10)

def run(cmd, timeout=30):
    _, stdout, _ = cli.exec_command(cmd, timeout=timeout)
    print(f"$ {cmd}")
    print(stdout.read().decode())

run("docker ps -a --format '{{.Names}} {{.Status}}' | grep -E '^sbx-'")
run("name=$(docker ps -a --format '{{.Names}}' | grep -E '^sbx-' | tail -1); docker logs $name 2>&1 | tail -25")
run("docker logs sandbox-edge-proxy 2>&1 | tail -12")
cli.close()
