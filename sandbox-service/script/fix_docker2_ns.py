"""修 docker2 的 containerd 命名空间隔离（moby -> moby2），重测 CRIU 往返。"""
import json
import time

import paramiko

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(HOST, username=USER, password=PWD, timeout=10)

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


print("===== 1. daemon.json 加 containerd 命名空间隔离 =====")
cfg = {
    "hosts": ["unix:///var/run/docker2.sock"],
    "data-root": "/var/lib/docker2",
    "exec-root": "/var/run/docker2",
    "pidfile": "/var/run/docker2.pid",
    "bridge": "docker2_0",
    "fixed-cidr": "10.222.0.0/24",
    "default-address-pools": [{"base": "10.223.0.0/16", "size": 24}],
    "storage-driver": "overlay2",
    "experimental": True,
    "iptables": True,
    "live-restore": True,
    "containerd-namespace": "moby2",
    "containerd-plugins-namespace": "moby2-plugins",
}
run(f"cat > /etc/docker2/daemon.json << 'EOF'\n{json.dumps(cfg, indent=2)}\nEOF")
run("dockerd --validate --config-file /etc/docker2/daemon.json")
run("systemctl restart docker2", timeout=180)
time.sleep(6)
run("systemctl is-active docker2")
run(f"{D} info --format 'Driver={{{{.Driver}}}}'")
run("ctr -n moby c ls | head -3; echo ---; ctr -n moby2 c ls | head -3")

print("===== 2. 镜像重新搬家（moby2 命名空间下镜像还在 graphdriver，不需重搬）=====")
run(f"{D} images --format '{{{{.Repository}}}}:{{{{.Tag}}}}'")

print("===== 3. CRIU 往返（自定义网络 criu-net）=====")
run(f"{D} network create criu-net 2>/dev/null")
run(f"{D} rm -f criu-t 2>/dev/null")
run(f"{D} run -d --name criu-t --network criu-net {IMG} {CMD}")
time.sleep(5)
run(f"{D} exec criu-t cat /tmp/cnt")
run(f"{D} checkpoint create --leave-running=false criu-t snap1", timeout=180)
run(f"{D} start --checkpoint snap1 criu-t", timeout=180)
time.sleep(3)
out = run(f"{D} exec criu-t cat /tmp/cnt")
try:
    n = int(out.splitlines()[-1])
    print(f"### 结果: {'✅ CRIU 成功（计数=' + str(n) + '）' if n >= 4 else '❌'}")
except Exception:
    print("### 结果: ❌ restore 失败")

print("===== 4. 若失败，尝试 ctr 内容清理后再次 restore =====")
run(f"{D} rm -f criu-t 2>/dev/null")
cli.close()
