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


print("===== 1. 排查残留的第二 dockerd 配置/进程 =====")
run("ls -l /etc/docker/ 2>&1")
run("systemctl list-units --all | grep -iE 'docker|containerd' | head -10")
run("ps aux | grep -E 'dockerd|containerd' | grep -v grep | head -10")

print("===== 2. 停 docker，清掉坏掉的 socket 目录 =====")
run("systemctl stop docker.service docker.socket 2>&1")
time.sleep(2)
run("ls -ld /run/docker.sock 2>&1")
run("rmdir /run/docker.sock 2>&1 && echo 'rmdir ok' || echo 'rmdir failed(非空?)'; ls -ld /run/docker.sock 2>&1")

print("===== 3. 重启 docker 栈 =====")
run("systemctl start docker.socket docker.service 2>&1")
time.sleep(5)
run("systemctl is-active docker.service docker.socket containerd")
run("ls -ld /run/docker.sock 2>&1")
run("docker version --format 'Server={{.Server.Version}}' 2>&1")
run("docker ps -a --format '{{.Names}}\\t{{.Status}}' 2>&1 | head -20")

cli.close()
