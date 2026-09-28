import time

import paramiko

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


print("=== 1. 先停掉第二 daemon，避免抢占默认 socket ===")
run("systemctl stop docker-sbx")
run("sleep 3")
print("主 daemon 容器（应能看到 mysql + sandbox-*）:")
run("docker ps --format '{{.Names}}' | head -10")

print()
print("=== 2. 修正 daemon.json：hosts 数组 ===")
run("""cat > /etc/docker-sbx/daemon.json <<'EOF'
{
  "experimental": true,
  "features": { "containerd-snapshotter": false },
  "data-root": "/var/lib/docker-sbx",
  "hosts": ["unix:///var/run/docker-sbx.sock"],
  "bridge": "docker-sbx",
  "iptables": true,
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
EOF""")
run("systemctl daemon-reload && systemctl restart docker-sbx")
time.sleep(12)
print("active:", run("systemctl is-active docker-sbx")[0])
run("ls -l /var/run/docker-sbx.sock")
print("driver:", run("DOCKER_HOST=unix:///var/run/docker-sbx.sock docker info --format '{{.Driver}}'")[0])
run("DOCKER_HOST=unix:///var/run/docker-sbx.sock docker info | grep -iE 'storage driver|experimental'")
print()
print("=== 3. 确认主 daemon 不受影响 ===")
run("docker ps --format '{{.Names}}' | head -10")

print()
print("=== 4. CRIU 往返 ===")
D = "DOCKER_HOST=unix:///var/run/docker-sbx.sock docker"
run(f"{D} pull m.daocloud.io/docker.io/library/alpine:3.20 2>&1 | tail -2", timeout=300)
run(f"{D} rm -f criu-test >/dev/null 2>&1")
run(f"{D} run -d --name criu-test m.daocloud.io/docker.io/library/alpine:3.20 "
    "sh -c 'i=0; while true; do i=$((i+1)); sleep 1; done'", timeout=120)
run("sleep 3")
run(f"{D} exec criu-test sh -c 'echo hello-criu > /tmp/state.txt; cat /tmp/state.txt'")
print("\n--- dump ---")
out, err = run(f"{D} checkpoint create --leave-running=false criu-test cp1 2>&1", timeout=180)
print("dump:", out or err)
run(f"{D} ps -a --format '{{{{.Names}}}} {{{{.Status}}}}' | grep criu-test")
print("\n--- restore ---")
out, err = run(f"{D} start --checkpoint cp1 criu-test 2>&1", timeout=180)
print("restore:", out or err)
run("sleep 5")
run(f"{D} ps -a --format '{{{{.Names}}}} {{{{.Status}}}}' | grep criu-test")
out, _ = run(f"{D} exec criu-test cat /tmp/state.txt 2>&1")
print("state after restore:", repr(out))
ok = "hello-criu" in out
print("CRIU ROUNDTRIP:", "OK" if ok else "FAILED")
run(f"{D} checkpoint ls criu-test 2>&1")
run(f"{D} rm -f criu-test >/dev/null 2>&1")
print()
print("RESULT:", "CRIU_OK" if ok else "CRIU_FAIL")
cli.close()
