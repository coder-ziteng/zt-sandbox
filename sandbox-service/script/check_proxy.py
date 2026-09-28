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
        print("STDERR:", e[-800:])

run("docker ps -a --format '{{.Names}} {{.Status}}' | grep sandbox")
run("docker logs sandbox-edge-proxy 2>&1 | tail -30")
run("ss -tlnp | grep -E ':443|:8902' || echo nothing-listening")
cli.close()
