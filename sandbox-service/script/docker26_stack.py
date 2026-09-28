"""给 docker2 换 Docker 26 独立二进制（主 daemon 的 29 不动），重测 CRIU 往返。

docker2 已经是独立 daemon（独立 socket/data-root/网桥/命名空间），
这一步只换 ExecStart 的二进制路径 + 验证与共享 containerd 的兼容性。
"""
import time

import paramiko

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(HOST, username=USER, password=PWD, timeout=10)

D = "docker -H unix:///var/run/docker2.sock"
IMG = "m.daocloud.io/docker.io/library/python:3.11-slim"
CMD = "sh -c 'i=0; while true; do i=$((i+1)); echo $i > /tmp/cnt; sleep 1; done'"


def run(cmd, timeout=300):
    _, o, e = cli.exec_command(cmd, timeout=timeout)
    rc = o.channel.recv_exit_status()
    out = o.read().decode("utf-8", "ignore").strip()
    err = e.read().decode("utf-8", "ignore").strip()
    print("$", cmd[:150])
    if out:
        print(out[:800])
    if err:
        print("[err]", err[:400])
    print()
    return rc, out


print("===== 1. 下载 docker 26 静态二进制（双镜像源兜底）=====")
rc, out = run("curl -sL -m 240 -o /tmp/docker26.tgz "
              "https://mirrors.aliyun.com/docker-ce/linux/static/stable/x86_64/docker-26.1.4.tgz "
              "&& ls -la /tmp/docker26.tgz", timeout=300)
if rc != 0:
    rc, out = run("curl -sL -m 240 -o /tmp/docker26.tgz "
                  "https://download.docker.com/linux/static/stable/x86_64/docker-26.1.4.tgz "
                  "&& ls -la /tmp/docker26.tgz", timeout=300)
if rc != 0:
    print("!! 下载失败")
    raise SystemExit(1)

run("mkdir -p /opt/docker26 && tar xzf /tmp/docker26.tgz -C /opt/docker26 --strip-components=1 "
    "&& /opt/docker26/dockerd --version && /opt/docker26/docker --version")

print("===== 2. 切换 docker2 的 ExecStart 到 docker26 =====")
unit = """[Unit]
Description=Docker Application Container Engine (instance 2, CRIU-capable, overlay2)
After=network-online.target containerd.service
Wants=network-online.target

[Service]
Type=notify
ExecStartPre=-/sbin/ip link add name docker2_0 type bridge
ExecStartPre=-/sbin/ip addr add 10.222.0.1/24 dev docker2_0
ExecStartPre=/sbin/ip link set docker2_0 up
ExecStart=/opt/docker26/dockerd --config-file /etc/docker2/daemon.json --containerd=/run/containerd/containerd.sock
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
run("systemctl daemon-reload && systemctl restart docker2", timeout=180)
time.sleep(8)
run("systemctl is-active docker2")
rc, out = run(f"{D} version --format 'Server={{{{.Server.Version}}}}'")
if rc != 0:
    print("!! docker26 与共享 containerd 2.x 不兼容，日志：")
    run("journalctl -u docker2 --no-pager -n 15 | tail -12")
    raise SystemExit(2)
run(f"{D} info --format 'Driver={{{{.Driver}}}} Experimental={{{{.ExperimentalBuild}}}}'")
run("docker ps --format '{{.Names}}' | head -3")  # 主 daemon 无恙确认

print("===== 3. CRIU 真实往返 =====")
run(f"{D} rm -f criu-t 2>/dev/null")
run(f"{D} run -d --name criu-t --network criu-net {IMG} {CMD}")
time.sleep(5)
run(f"{D} exec criu-t cat /tmp/cnt")
run(f"{D} checkpoint create --leave-running=false criu-t snap1", timeout=180)
run(f"{D} start --checkpoint snap1 criu-t", timeout=180)
time.sleep(3)
rc, out = run(f"{D} exec criu-t cat /tmp/cnt")
try:
    n = int(out.splitlines()[-1])
    print(f"### 结果: {'✅✅ 真 CRIU 成功！计数器断点续跑 n=' + str(n) if n >= 4 else '❌ 计数异常'}")
except Exception:
    print("### 结果: ❌ restore 仍失败")
run(f"{D} rm -f criu-t 2>/dev/null")
cli.close()
