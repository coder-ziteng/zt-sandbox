"""CRIU restore netns 报错测试矩阵：
A) --network none（无 netns 问题？）
B) 默认网桥 + 预 touch SandboxKey 文件再 restore
C) 用户自定义网络 + 预 touch SandboxKey 文件再 restore
"""
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
        print(out[:600])
    if err:
        print("[err]", err[:300])
    print()
    return out


def trial(name, network_args, pretouch):
    print(f"########## 方案 {name} ##########")
    run(f"{D} rm -f criu-t 2>/dev/null")
    run(f"{D} run -d --name criu-t {network_args} {IMG} {CMD}")
    time.sleep(5)
    run(f"{D} exec criu-t cat /tmp/cnt")
    run(f"{D} checkpoint create --leave-running=false criu-t snap1", timeout=180)
    if pretouch:
        out = run(f"{D} inspect -f '{{{{.NetworkSettings.SandboxKey}}}}' criu-t")
        key = out.splitlines()[-1].strip() if out else ""
        print("SandboxKey =", key)
        if key and key != "no value":
            run(f"mkdir -p /var/run/docker2/netns && touch {key} && ls -la {key}")
    run(f"{D} start --checkpoint snap1 criu-t", timeout=180)
    time.sleep(3)
    out = run(f"{D} exec criu-t cat /tmp/cnt")
    ok = False
    try:
        n = int(out.splitlines()[-1])
        ok = n >= 4
    except Exception:
        pass
    print(f"### 方案 {name}: {'✅ 成功（计数=' + out.splitlines()[-1] + '）' if ok else '❌ 失败'}\n")
    run(f"{D} rm -f criu-t 2>/dev/null")
    return ok


trial("A: --network none", "--network none", pretouch=False)
trial("B: 默认网桥 + 预touch netns", "", pretouch=True)
run(f"{D} network create criu-net 2>/dev/null")
trial("C: 自定义网络 + 预touch netns", "--network criu-net", pretouch=True)
cli.close()
