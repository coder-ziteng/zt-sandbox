import paramiko

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(HOST, username=USER, password=PWD, timeout=10)


def run(cmd, timeout=60):
    _, o, e = cli.exec_command(cmd, timeout=timeout)
    out = o.read().decode("utf-8", "ignore").strip()
    err = e.read().decode("utf-8", "ignore").strip()
    print("$", cmd)
    if out:
        print(out)
    if err:
        print("[err]", err[:400])
    print()


run("ls -ld /var/run/docker.sock /run/docker.sock 2>&1")
run("systemctl is-active docker.service docker.socket containerd 2>&1")
run("docker ps -a --format '{{.Names}}\\t{{.Status}}\\t{{.Ports}}' 2>&1 | head -20")
run("docker info --format 'ServerVersion={{.ServerVersion}} Driver={{.Driver}} CgroupDriver={{.CgroupDriver}}' 2>&1")
run("ss -ltnp | grep -E ':8902|:443|:49999' | head -10")
run("curl -s -m 5 -o /dev/null -w 'cp_health=%{http_code}\\n' http://127.0.0.1:8902/health 2>&1")
cli.close()
