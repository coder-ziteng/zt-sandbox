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
        print("STDERR:", e[-500:])

run("systemctl is-active firewalld || echo no-firewalld")
run("firewall-cmd --permanent --add-port=8902/tcp --add-port=443/tcp --add-port=20000-21000/tcp 2>&1 || true")
run("firewall-cmd --reload 2>&1 || true")
run("iptables -L INPUT -n | head -5")
cli.close()
