"""验证：把 docker2 的 netns 目录做成共享挂载后，CRIU restore 是否成功。"""
import time

import paramiko

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect("<internal-host>", username="root", password="<redacted>", timeout=10)

D = "docker -H unix:///var/run/docker2.sock"
IMG = "m.daocloud.io/docker.io/library/python:3.11-slim"
CMD = "sh -c 'i=0; while true; do i=$((i+1)); echo $i > /tmp/cnt; sleep 1; done'"


def run(cmd, timeout=120):
    _, o, e = cli.exec_command(cmd, timeout=timeout)
    out = o.read().decode("utf-8", "ignore").strip()
    err = e.read().decode("utf-8", "ignore").strip()
    print("$", cmd[:140])
    if out:
        print(out[:700])
    if err:
        print("[err]", err[:400])
    print()
    return out


print("===== 对照：主 daemon 的 netns 目录挂载情况 =====")
run("findmnt /run/docker/netns; ls -la /run/docker/netns | head -3")
run("findmnt /var/run/docker2/netns; ls -la /var/run/docker2/netns 2>&1 | head -3")

print("===== 建容器 + checkpoint =====")
run(f"{D} rm -f criu-t 2>/dev/null")
run(f"{D} run -d --name criu-t --network criu-net {IMG} {CMD}")
time.sleep(5)
run(f"{D} exec criu-t cat /tmp/cnt")
run("findmnt /var/run/docker2/netns")
run(f"{D} checkpoint create --leave-running=false criu-t snap1", timeout=180)

print("===== 把 netns 目录做成共享挂载，再 restore =====")
run("mkdir -p /var/run/docker2/netns")
run("mount --bind /var/run/docker2/netns /var/run/docker2/netns && mount --make-rshared /var/run/docker2/netns")
run("findmnt /var/run/docker2/netns")
run(f"{D} start --checkpoint snap1 criu-t", timeout=180)
time.sleep(3)
out = run(f"{D} exec criu-t cat /tmp/cnt")
try:
    n = int(out.splitlines()[-1])
    print(f"### 结果: {'✅ CRIU 成功（计数=' + str(n) + '，断点续跑）' if n >= 4 else '❌ 计数异常'}")
except Exception:
    print("### 结果: ❌ restore 失败")
run(f"{D} rm -f criu-t 2>/dev/null")
cli.close()
