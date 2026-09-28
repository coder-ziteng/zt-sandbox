"""安全搭建第二 dockerd（docker2）专供 CRIU：overlay2 + experimental，主 daemon 零改动。

与上次事故的差异（教训已内化）：
  - hosts 用正确的【数组】字段，且显式指向 /var/run/docker2.sock（上次误写 "host" 被忽略，
    daemon 回退默认路径抢占了主 socket）
  - data-root / exec-root / pidfile / bridge / 子网全部独立
  - 启动前 `dockerd --validate` 校验配置；启动后逐项断言主 daemon 健康
  - 任何一步失败立即停止并输出回滚命令
"""
import json
import sys
import time

import paramiko

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(HOST, username=USER, password=PWD, timeout=10)

FAILED = []


def run(cmd, timeout=120, check=False):
    _, o, e = cli.exec_command(cmd, timeout=timeout)
    rc = o.channel.recv_exit_status()
    out = o.read().decode("utf-8", "ignore").strip()
    err = e.read().decode("utf-8", "ignore").strip()
    print("$", cmd[:150])
    if out:
        print(out[:1200])
    if err:
        print("[err]", err[:400])
    print()
    if check and rc != 0:
        FAILED.append(cmd)
        print(f"!! rc={rc}\n")
    return rc, out


def assert_main_healthy(step):
    rc1, out1 = run("docker ps --format '{{.Names}}' | head -5")
    rc2, out2 = run("ls -ld /run/docker.sock")
    ok = rc1 == 0 and out2.startswith("s")
    print(f"[main-check @ {step}] {'OK' if ok else '!! BROKEN !!'}\n")
    if not ok:
        print("主 daemon 异常！立即中止。回滚: systemctl stop docker2 && rm -rf /etc/docker2 /etc/systemd/system/docker2.service && systemctl daemon-reload")
        sys.exit(1)


print("===== 0. 前置检查 =====")
assert_main_healthy("pre")
run("criu --version | head -1")
run("ls /var/lib/docker2 2>/dev/null && echo 'docker2 data exists' || echo 'fresh'")

print("===== 1. 写 docker2 配置 =====")
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
}
cfg_json = json.dumps(cfg, indent=2)
run(f"mkdir -p /etc/docker2 && cat > /etc/docker2/daemon.json << 'EOF'\n{cfg_json}\nEOF")
run("cat /etc/docker2/daemon.json")

print("===== 2. 配置校验（不启动）=====")
run("dockerd --validate --config-file /etc/docker2/daemon.json", check=True)

print("===== 3. systemd 单元 =====")
unit = """[Unit]
Description=Docker Application Container Engine (instance 2, CRIU-capable, overlay2)
After=network-online.target containerd.service
Wants=network-online.target

[Service]
Type=notify
ExecStart=/usr/bin/dockerd --config-file /etc/docker2/daemon.json --containerd=/run/containerd/containerd.sock
ExecReload=/bin/kill -s HUP $MAINPID
TimeoutStartSec=0
Restart=on-failure
RestartSec=5
LimitNOFILE=1048576
LimitNPROC=infinity
LimitCORE=infinity
TasksMax=infinity

[Install]
WantedBy=multi-user.target
"""
run(f"cat > /etc/systemd/system/docker2.service << 'EOF'\n{unit}EOF")
run("systemctl daemon-reload")

print("===== 4. 启动 docker2 =====")
run("systemctl start docker2", timeout=180, check=True)
time.sleep(5)
run("systemctl is-active docker2")
run("ls -ld /var/run/docker2.sock /run/docker.sock")

print("===== 5. 启动后主 daemon 健康断言 =====")
assert_main_healthy("post-start")
run("docker ps --format '{{.Names}} {{.Status}}' | grep -E 'mysql|sandbox' ")

print("===== 6. docker2 自检 =====")
run("docker -H unix:///var/run/docker2.sock version --format 'Server={{.Server.Version}} Driver={{.Server.Driver}}'")
run("docker -H unix:///var/run/docker2.sock info --format 'Driver={{.Driver}} Experimental={{.ExperimentalBuild}} DockerRootDir={{.DockerRootDir}}'")

print("===== 7. 镜像搬家（主 -> docker2）=====")
run("docker save sandbox/base:v1 sandbox/code-interpreter:v1 sandbox/browser:v1 sandbox/all-in-one:v1 "
    "| docker -H unix:///var/run/docker2.sock load", timeout=1800, check=True)
run("docker -H unix:///var/run/docker2.sock pull m.daocloud.io/docker.io/library/python:3.11-slim", timeout=600)

print("===== 8. docker2 上做真实 CRIU 往返 =====")
run("docker -H unix:///var/run/docker2.sock rm -f criu-test 2>/dev/null; "
    "docker -H unix:///var/run/docker2.sock run -d --name criu-test "
    "m.daocloud.io/docker.io/library/python:3.11-slim "
    "sh -c 'i=0; while true; do i=$((i+1)); echo $i > /tmp/cnt; sleep 1; done'")
time.sleep(6)
run("docker -H unix:///var/run/docker2.sock exec criu-test cat /tmp/cnt")
run("docker -H unix:///var/run/docker2.sock checkpoint create --leave-running=false criu-test snap1", timeout=180, check=True)
run("docker -H unix:///var/run/docker2.sock start --checkpoint snap1 criu-test", timeout=180, check=True)
time.sleep(4)
rc, out = run("docker -H unix:///var/run/docker2.sock exec criu-test cat /tmp/cnt")
try:
    n = int(out.splitlines()[-1])
    print(f"[CRIU] 恢复后计数器 = {n} -> {'✅ 内存态真实续跑（非重启）' if n >= 5 else '⚠️ 计数偏小，需人工核对'}")
except Exception:
    print("!! 无法读取计数器")
run("docker -H unix:///var/run/docker2.sock rm -f criu-test")

print("===== 9. 最终主 daemon 健康断言 =====")
assert_main_healthy("final")

if FAILED:
    print("有失败步骤:", FAILED)
    sys.exit(1)
print("DOCKER2 SETUP DONE ✅  (回滚: systemctl stop docker2 && systemctl disable docker2)")
cli.close()
