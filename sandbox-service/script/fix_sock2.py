import time

import paramiko

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect("<internal-host>", username="root", password="<redacted>", timeout=10)


def run(cmd, timeout=120):
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


# 只删空目录，非空 rmdir 会自动失败，不会误删数据
run("rmdir /run/docker.sock 2>&1 || echo 'rmdir failed (not empty or not a dir)'")
run("ls -ld /run/docker.sock 2>&1")
run("systemctl restart docker")
time.sleep(12)
run("ls -ld /var/run/docker.sock")
run("docker ps -a --format '{{.Names}}\t{{.Status}}'")
run("curl -s http://127.0.0.1:8902/health; echo")
run("docker images --format '{{.Repository}}:{{.Tag}}' | head -12")
cli.close()
print("RECOVERY DONE")
