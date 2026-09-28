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
        print("[err]", err[:400])
    print()
    return out


run("ls -ld /var/run/docker.sock /run/docker.sock 2>&1")
run("ls -la /var/run/docker.sock 2>&1 | head -10")
run("readlink -f /var/run; ls -ld /var/run")
run("ps aux | grep -c '[d]ockerd'")
run("ss -xl 2>/dev/null | grep docker || netstat -xl 2>/dev/null | grep docker")
cli.close()
