import paramiko
cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect("<internal-host>", username="root", password="<redacted>", timeout=10)

def run(cmd, timeout=60):
    _, stdout, stderr = cli.exec_command(cmd, timeout=timeout)
    rc = stdout.channel.recv_exit_status()
    print(f"$ {cmd}\n[rc={rc}]\n{stdout.read().decode()}")
    e = stderr.read().decode()
    if e.strip():
        print("STDERR:", e[-800:])

run("docker logs sandbox-control-plane 2>&1 | tail -40")
run("docker ps -a --format '{{.Names}} {{.Status}}' | grep -E 'sandbox|sbx' | head")
cli.close()
