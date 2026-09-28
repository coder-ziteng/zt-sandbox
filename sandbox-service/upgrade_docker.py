"""upgrade_docker.py -- 将 <internal-host> 上的 Docker 从 29.5.2 升级到最新 29.x

步骤:
1. dnf upgrade docker-ce + containerd.io + runc + docker-buildx-plugin
2. 保留 daemon.json 配置（containerd-snapshotter: false）
3. 重启 Docker, compose up
4. 重新 CRIU 往返测试

⚠️ 升级期间所有沙箱容器会暂停，管控面会短暂不可用。
"""
import sys
import time

import paramiko

HOST, USER, PWD = "<internal-host>", "root", "123456"
REMOTE_DIR = "/srv/sandbox-service"

sys.path.insert(0, ".")
from deploy_server import API_KEYS


def ssh_connect():
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(HOST, username=USER, password=PWD, timeout=15)
    return cli


def run(cli, cmd, timeout=600, check=True):
    print(f"\n  $ {cmd}")
    _, stdout, stderr = cli.exec_command(cmd, timeout=timeout)
    rc = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    if out.strip():
        print(f"    {out.strip()[-1000:]}")
    if err.strip():
        print(f"    [stderr] {err.strip()[-500:]}")
    print(f"    [rc={rc}]")
    if check and rc != 0:
        raise SystemExit(f"failed (rc={rc}): {cmd}")
    return rc, out, err


def criu_roundtrip(cli):
    """与 fix_criu.py 中的测试一致：host networking + python:3.11-slim"""
    print("\n" + "=" * 60)
    print("  CRIU round-trip self-test")
    print("=" * 60)

    img = "m.daocloud.io/docker.io/library/python:3.11-slim"
    name = "sbx-criu-verify"
    run(cli, f"docker rm -f {name} 2>/dev/null", check=False)
    run(cli, f"docker pull {img}", timeout=180, check=False)
    rc, _, _ = run(cli, f"docker run -d --name {name} --network=host {img} sleep 120", check=False)
    if rc != 0:
        return False
    time.sleep(2)

    # Checkpoint
    rc, out, err = run(cli, f"docker checkpoint create {name} sbx-snap 2>&1", timeout=120, check=False)
    if rc != 0 or "Error" in out:
        print("  [X] DUMP failed")
        run(cli, f"docker rm -f {name}", check=False)
        return False
    print("  [ok] dump ok")

    # Restore
    rc, out, err = run(cli, f"docker start --checkpoint sbx-snap {name} 2>&1", timeout=120, check=False)
    if rc != 0 or "Error" in out:
        print("  [X] RESTORE failed")
        if "already exists" in (out + err):
            print("    --> containerd content-store collision (Docker bug)")
        elif "does not exist" in (out + err):
            print("    --> checkpoint not found")
        run(cli, f"docker rm -f {name}", check=False)
        return False

    time.sleep(2)
    rc, out, _ = run(cli, f"docker inspect -f '{{{{.State.Running}}}}' {name}", check=False)
    running = out.strip() == "true"
    print(f"  [{'ok' if running else 'X'}] running after restore: {out.strip()}")
    run(cli, f"docker rm -f {name}", check=False)
    return running


def main():
    print("=" * 60)
    print("upgrade_docker.py -- Docker CE 升级")
    print("=" * 60)

    cli = ssh_connect()

    try:
        # Current version
        print("\n[1/6] 当前 Docker 版本:")
        rc, out, _ = run(cli, "docker version --format 'Server: {{.Server.Version}}'", check=False)
        print(f"    {out.strip()}")

        # 1. Stop all sandboxes
        print("\n[2/6] 清理现有沙箱...")
        run(cli, "docker rm -f $(docker ps --filter name=sbx- -q) 2>/dev/null", check=False)
        time.sleep(2)

        # 2. Upgrade Docker (preserve daemon.json)
        print("\n[3/6] 升级 Docker CE...")
        daemon_backup = "/tmp/daemon.json.bak"
        run(cli, f"cp /etc/docker/daemon.json {daemon_backup}", check=False)
        run(cli, "dnf upgrade -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin",
            timeout=600)
        run(cli, f"cp {daemon_backup} /etc/docker/daemon.json", check=False)
        print("    daemon.json 已恢复")

        # 3. Restart Docker
        print("\n[4/6] 重启 Docker...")
        run(cli, "systemctl restart docker", timeout=60)
        time.sleep(6)

        # 4. Verify
        print("\n[5/6] 验证新版本...")
        rc, out, _ = run(cli, "docker version --format 'Server: {{.Server.Version}}'", check=False)
        print(f"    {out.strip()}")
        rc, out, _ = run(cli, "docker info 2>&1 | grep -E 'Storage Driver|containerd snapshotter'", check=False)
        print(f"    {out.strip()}")

        # 5. Compose up
        print("\n[6/6] 重启管控面...")
        run(cli, f"cd {REMOTE_DIR}/deploy && docker compose up -d", timeout=300)
        time.sleep(5)

        # Wait for control-plane
        for i in range(30):
            rc, out, _ = run(cli, "curl -sf http://127.0.0.1:8902/health", check=False)
            if rc == 0 and out.strip():
                print(f"    [ok] control-plane healthy: {out.strip()}")
                break
            time.sleep(2)
        else:
            print("    [X] control-plane didn't come up")
            run(cli, "docker logs sandbox-control-plane --tail 30", check=False)
            return 1

        # 6. CRIU test
        ok_criu = criu_roundtrip(cli)

    finally:
        cli.close()

    print("\n" + "=" * 60)
    if ok_criu:
        print("[PASS] CRIU checkpoint/restore 往返测试通过!")
        print("       真 CRIU 暂停恢复已激活")
    else:
        print("[FAIL] CRIU 往返测试仍然失败")
        print("       可能需要更大幅度的 Docker 版本变更")
    print("=" * 60)
    return 0 if ok_criu else 1


if __name__ == "__main__":
    sys.exit(main())
