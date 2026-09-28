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
        print(out[:3000])
    if err:
        print("[err]", err[:800])
    print()


run("systemctl status docker2 --no-pager | head -15")
run("journalctl -u docker2 --no-pager -n 40 | tail -35")
cli.close()
