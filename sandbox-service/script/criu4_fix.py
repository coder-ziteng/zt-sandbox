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
            print("[stderr]", err[:800])
    return out, err


print("=== fix: 独立 pidfile / exec-root ===")
run("""cat > /etc/systemd/system/docker-sbx.service <<'EOF'
[Unit]
Description=Sandbox Docker Daemon (native graphdriver, CRIU capable)
After=network.target
Wants=network-online.target

[Service]
Type=notify
ExecStart=/usr/bin/dockerd \\
  --config-file /etc/docker-sbx/daemon.json \\
  --pidfile /var/run/docker-sbx.pid \\
  --exec-root /var/run/docker-sbx-exec
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
run("systemctl daemon-reload && systemctl restart docker-sbx")
time.sleep(8)

out, err = run("systemctl is-active docker-sbx")
print("active:", out or err)
out, _ = run("DOCKER_HOST=unix:///var/run/docker-sbx.sock docker info --format '{{.Driver}} | {{.DriverStatus}}'")
print("driver:", out)
run("DOCKER_HOST=unix:///var/run/docker-sbx.sock docker info | grep -iE 'storage driver|experimental|server version'")

print()
print("=== CRIU 往返测试 ===")
D = "DOCKER_HOST=unix:///var/run/docker-sbx.sock docker"
run(f"{D} pull m.daocloud.io/docker.io/library/alpine:3.20 2>&1 | tail -2", timeout=300)
run(f"{D} rm -f criu-test >/dev/null 2>&1")
run(f"{D} run -d --name criu-test m.daocloud.io/docker.io/library/alpine:3.20 sh -c 'while true; do sleep 1; done'",
    timeout=120)
run("sleep 3")
run(f"{D} exec criu-test sh -c 'echo hello-criu > /tmp/state.txt; echo 12345 > /tmp/counter.txt; cat /tmp/state.txt'")

print("\n--- checkpoint create ---")
out, err = run(f"{D} checkpoint create --leave-running=false criu-test cp1 2>&1", timeout=180)
print("checkpoint:", out or err)
run(f"{D} ps -a --format '{{{{.Names}}}} {{{{.Status}}}}' | grep criu-test")

print("\n--- restore ---")
out, err = run(f"{D} start --checkpoint cp1 criu-test 2>&1", timeout=180)
print("restore:", out or err)
run("sleep 5")
run(f"{D} ps -a --format '{{{{.Names}}}} {{{{.Status}}}}' | grep criu-test")
out, _ = run(f"{D} exec criu-test cat /tmp/state.txt 2>&1")
print("state after restore:", repr(out))
print("CRIU ROUNDTRIP:", "OK" if "hello-criu" in out else "FAILED")

print("\n--- checkpoint 列表 & 清理 ---")
run(f"{D} checkpoint ls criu-test 2>&1")
run(f"{D} rm -f criu-test >/dev/null 2>&1")
run("du -sh /var/lib/docker-sbx 2>/dev/null")
cli.close()
