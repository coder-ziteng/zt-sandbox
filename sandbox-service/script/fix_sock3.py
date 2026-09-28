import paramiko

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect("<internal-host>", username="root", password="<redacted>", timeout=10)


def run(cmd, timeout=60):
    _, o, e = cli.exec_command(cmd, timeout=timeout)
    out = o.read().decode("utf-8", "ignore").strip()
    err = e.read().decode("utf-8", "ignore").strip()
    print("$", cmd)
    if out:
        print(out)
    if err:
        print("[err]", err[:300])
    print()


run("cat /etc/docker/daemon.json")
run("systemctl cat docker.service | head -30")
run("systemctl cat docker.socket 2>&1 | head -20")
run("systemctl is-active docker.socket; systemctl is-enabled docker.socket 2>&1")
run("ls -ld /run/docker /run/docker/* 2>&1 | head -10")
run("journalctl -u docker.service --no-pager -n 8 | grep -iE 'listen|API|error' | tail -6")
cli.close()
