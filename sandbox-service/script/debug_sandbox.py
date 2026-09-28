import paramiko
cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(HOST, username=USER, password=PWD, timeout=10)

def run(cmd, timeout=60):
    _, stdout, stderr = cli.exec_command(cmd, timeout=timeout)
    rc = stdout.channel.recv_exit_status()
    print(f"$ {cmd}\n[rc={rc}]\n{stdout.read().decode()}")
    e = stderr.read().decode()
    if e.strip():
        print("STDERR:", e[-600:])

run("docker ps -a --format '{{.Names}} {{.Status}}' | grep -E 'sandbox|^sbx'")
run("docker logs sandbox-edge-proxy 2>&1 | tail -6")
run("docker inspect sbx-sbx66aa7cccfd0040b0a --format '{{.State.Status}} {{.State.ExitCode}}' 2>/dev/null || true")
run("docker logs sbx-sbx66aa7cccfd0040b0a 2>&1 | tail -20 2>/dev/null || true")
run("ss -tlnp | grep -E ':2000[0-9]|:200[0-9][0-9]' | head -5")
run("curl -s -m 3 http://127.0.0.1:20002/health && echo <-envd || echo envd-down")
cli.close()
