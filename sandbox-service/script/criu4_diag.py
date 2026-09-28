import paramiko

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect("<internal-host>", username="root", password="<redacted>", timeout=10)


def run(cmd, timeout=60):
    _, o, e = cli.exec_command(cmd, timeout=timeout)
    print("$", cmd)
    print(o.read().decode("utf-8", "ignore").strip())
    err = e.read().decode("utf-8", "ignore").strip()
    if err:
        print("[err]", err[:1500])
    print()


run("systemctl status docker-sbx.service --no-pager | head -20")
run("journalctl -xeu docker-sbx.service --no-pager -n 25")
run("cat /etc/docker-sbx/daemon.json")
run("dockerd --validate --config-file /etc/docker-sbx/daemon.json 2>&1 | head -10")
run("dockerd --version")
cli.close()
