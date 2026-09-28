"""紧急恢复：重启主 dockerd 并确认 mysql / sandbox 服务回来。同时停用 docker-sbx。"""
import time

import paramiko

HOST, USER, PWD = "<internal-host>", "root", "123456"
cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(HOST, username=USER, password=PWD, timeout=10)


def run(cmd, timeout=180):
    _, o, e = cli.exec_command(cmd, timeout=timeout)
    out = o.read().decode("utf-8", "ignore").strip()
    err = e.read().decode("utf-8", "ignore").strip()
    print("$", cmd)
    if out:
        print(out)
    if err:
        print("[err]", err[:600])
    print()
    return out


run("systemctl stop docker-sbx && systemctl disable docker-sbx")
run("systemctl restart docker")
time.sleep(10)
run("ls -l /var/run/docker.sock")
run("docker ps -a --format '{{.Names}}\t{{.Status}}'")
run("curl -s http://127.0.0.1:8902/health")
run("docker images --format '{{.Repository}}:{{.Tag}}' | head -12")
cli.close()
print("RECOVERY DONE")
