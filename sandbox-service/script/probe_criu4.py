"""可行性验证：在服务器上另起一个独立 dockerd（独立 data-root / socket / 关闭
containerd-snapshotter），在其上做一次真实的 CRIU checkpoint -> restore 往返。

不触碰现有 dockerd 与 mysql：新 daemon 用 /var/lib/docker-sbx + docker-sbx.sock。
全部操作可回滚（停掉 unit、删掉目录即可）。
"""
import json
import time

import paramiko

HOST, USER, PWD = "<internal-host>", "root", "123456"
cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(HOST, username=USER, password=PWD, timeout=10)


def run(cmd, timeout=180, show=True):
    _, o, e = cli.exec_command(cmd, timeout=timeout)
    out = o.read().decode("utf-8", "ignore").strip()
    err = e.read().decode("utf-8", "ignore").strip()
    if show:
        if out:
            print(out)
        if err:
            print("[stderr]", err[:500])
    return out, err


print("=" * 60)
print("STEP 1: 启动第二 dockerd（独立 data-root/socket，关闭 containerd-snapshotter）")
print("=" * 60)
run("mkdir -p /var/lib/docker-sbx /etc/docker-sbx")
run("""cat > /etc/docker-sbx/daemon.json <<'EOF'
{
  "experimental": true,
  "features": { "containerd-snapshotter": false },
  "data-root": "/var/lib/docker-sbx",
  "host": "unix:///var/run/docker-sbx.sock",
  "bridge": "docker-sbx",
  "iptables": true,
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
EOF""")
run("""cat > /etc/systemd/system/docker-sbx.service <<'EOF'
[Unit]
Description=Sandbox Docker Daemon (native graphdriver, CRIU capable)
After=network.target docker.service
Wants=network-online.target

[Service]
Type=notify
ExecStart=/usr/bin/dockerd --config-file /etc/docker-sbx/daemon.json
ExecReload=/bin/kill -s HUP $MAINPID
TimeoutSec=0
RestartSec=2
Restart=always
LimitNOFILE=infinity
Delegate=yes
KillMode=process

[Install]
WantedBy=multi-user.target
EOF""")
run("systemctl daemon-reload && systemctl enable docker-sbx >/dev/null 2>&1; systemctl restart docker-sbx")
time.sleep(6)

out, _ = run("DOCKER_HOST=unix:///var/run/docker-sbx.sock docker info --format '{{.Driver}} | {{.DriverStatus}}'")
run("DOCKER_HOST=unix:///var/run/docker-sbx.sock docker info | grep -iE 'storage driver|experimental|server version'")
print("\n--> 第二 daemon driver:", out)

print()
print("=" * 60)
print("STEP 2: 拉一个轻镜像并做 CRIU checkpoint -> restore 往返")
print("=" * 60)
D = "DOCKER_HOST=unix:///var/run/docker-sbx.sock docker"
# 用已有的本地镜像做 tag 导入不现实（跨 daemon 不共享），直接 pull 一个小的
run(f"{D} pull m.daocloud.io/docker.io/library/alpine:3.20 2>&1 | tail -2", timeout=300)
run(f"{D} tag m.daocloud.io/docker.io/library/alpine:3.20 alpine:3.20")
run(f"{D} rm -f criu-test >/dev/null 2>&1")
run(f"{D} run -d --name criu-test alpine:3.20 sh -c 'i=0; while true; do i=$((i+1)); sleep 1; done'", timeout=120)
run("sleep 3")
run(f"{D} exec criu-test sh -c 'echo hello-criu > /tmp/state.txt; cat /tmp/state.txt'")

print("\n--- checkpoint create ---")
out, err = run(f"{D} checkpoint create --leave-running=false criu-test cp1 2>&1", timeout=180)
print("checkpoint rc-out:", out or err)
run(f"{D} ps -a --format '{{{{.Names}}}} {{{{.Status}}}}' | grep criu-test")

print("\n--- restore ---")
out, err = run(f"{D} start --checkpoint cp1 criu-test 2>&1", timeout=180)
print("restore out:", out or err)
run("sleep 4")
run(f"{D} ps -a --format '{{{{.Names}}}} {{{{.Status}}}}' | grep criu-test")
out, _ = run(f"{D} exec criu-test cat /tmp/state.txt 2>&1")
print("state after restore:", out)
print("CRIU ROUNDTRIP:", "OK" if "hello-criu" in out else "FAILED")

print("\n--- 清理 ---")
run(f"{D} rm -f criu-test >/dev/null 2>&1")
run(f"{D} images --format '{{{{.Repository}}}}:{{{{.Tag}}}} {{{{.Size}}}}'")
run("du -sh /var/lib/docker-sbx 2>/dev/null")

cli.close()
