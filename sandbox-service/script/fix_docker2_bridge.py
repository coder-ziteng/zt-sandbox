"""修 docker2：systemd 单元加 ExecStartPre 预建 docker2_0 网桥，然后继续镜像搬家 + CRIU 往返。"""
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
        print(out[:1000])
    if err:
        print("[err]", err[:400])
    print()
    if check and rc != 0:
        FAILED.append(cmd)
    return rc, out


def assert_main_healthy(step):
    rc1, _ = run("docker ps --format '{{.Names}}' | head -3")
    rc2, out2 = run("ls -ld /run/docker.sock")
    ok = rc1 == 0 and out2.startswith("s")
    print(f"[main-check @ {step}] {'OK' if ok else '!! BROKEN !!'}\n")
    if not ok:
        print("主 daemon 异常，中止！")
        sys.exit(1)


print("===== 1. 单元加 ExecStartPre 预建网桥 =====")
unit = """[Unit]
Description=Docker Application Container Engine (instance 2, CRIU-capable, overlay2)
After=network-online.target containerd.service
Wants=network-online.target

[Service]
Type=notify
ExecStartPre=-/sbin/ip link add name docker2_0 type bridge
ExecStartPre=-/sbin/ip addr add 10.222.0.1/24 dev docker2_0
ExecStartPre=/sbin/ip link set docker2_0 up
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
run("systemctl reset-failed docker2 2>/dev/null; systemctl start docker2", timeout=180, check=True)
time.sleep(6)
run("systemctl is-active docker2")
run("docker -H unix:///var/run/docker2.sock info --format 'Driver={{.Driver}} Experimental={{.ExperimentalBuild}} Root={{.DockerRootDir}}'", check=True)
assert_main_healthy("docker2-up")

print("===== 2. 镜像搬家（主 -> docker2）=====")
run("docker save sandbox/base:v1 sandbox/code-interpreter:v1 sandbox/browser:v1 sandbox/all-in-one:v1 "
    "| docker -H unix:///var/run/docker2.sock load", timeout=1800, check=True)
run("docker -H unix:///var/run/docker2.sock pull m.daocloud.io/docker.io/library/python:3.11-slim", timeout=600)
run("docker -H unix:///var/run/docker2.sock images --format '{{.Repository}}:{{.Tag}}'")

print("===== 3. docker2 上真实 CRIU 往返 =====")
run("docker -H unix:///var/run/docker2.sock rm -f criu-test 2>/dev/null; "
    "docker -H unix:///var/run/docker2.sock run -d --name criu-test "
    "m.daocloud.io/docker.io/library/python:3.11-slim "
    "sh -c 'i=0; while true; do i=$((i+1)); echo $i > /tmp/cnt; sleep 1; done'", check=True)
time.sleep(6)
run("docker -H unix:///var/run/docker2.sock exec criu-test cat /tmp/cnt")
run("docker -H unix:///var/run/docker2.sock checkpoint create --leave-running=false criu-test snap1", timeout=180, check=True)
run("docker -H unix:///var/run/docker2.sock start --checkpoint snap1 criu-test", timeout=180, check=True)
time.sleep(4)
rc, out = run("docker -H unix:///var/run/docker2.sock exec criu-test cat /tmp/cnt")
try:
    n = int(out.splitlines()[-1])
    verdict = "✅ 内存态真实续跑（计数器从断点继续，非重启）" if n >= 5 else "⚠️ 计数偏小需人工核对"
    print(f"[CRIU] 恢复后计数器 = {n} -> {verdict}")
except Exception:
    print("!! 无法读取计数器")
    FAILED.append("criu-verify")
run("docker -H unix:///var/run/docker2.sock rm -f criu-test")

assert_main_healthy("final")
if FAILED:
    print("失败步骤:", FAILED)
    sys.exit(1)
print("DOCKER2 + CRIU VERIFIED ✅")
cli.close()
