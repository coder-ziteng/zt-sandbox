"""finish_criu_fix.py -- continue from where fix_criu.py left off.

Base image already built. Build remaining images, compose up, register templates, test CRIU.
"""
import json
import os
import sys
import time

import paramiko

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from deploy_server import HOST, USER, PWD, REMOTE_DIR, API_KEYS

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _run(cli, cmd, timeout=600, check=True):
    print(f"\n  $ {cmd}")
    try:
        _, stdout, stderr = cli.exec_command(cmd, timeout=timeout)
        rc = stdout.channel.recv_exit_status()
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        if out.strip():
            print(f"    {out.strip()[:2000]}")
        if err.strip():
            print(f"    [stderr] {err.strip()[:1500]}")
        print(f"    [rc={rc}]")
        if check and rc != 0:
            raise SystemExit(f"failed (rc={rc}): {cmd}")
        return rc, out, err
    except SystemExit:
        raise
    except Exception as e:
        if not check:
            print(f"    [error] {e}")
            return -1, "", str(e)
        raise


def criu_roundtrip(cli):
    print("\n" + "=" * 60)
    print("  CRIU round-trip self-test")
    print("=" * 60)
    img = "m.daocloud.io/docker.io/library/alpine:3.20"
    name = "sbx-criu-verify"
    _run(cli, f"docker rm -f {name} 2>/dev/null", check=False)
    _run(cli, f"docker pull {img}", timeout=180, check=False)
    rc, _, _ = _run(cli, f"docker run -d --name {name} {img} sleep 60", check=False)
    if rc != 0:
        return False
    time.sleep(2)
    _run(cli, f"docker checkpoint rm {name} sbx-snap 2>/dev/null", check=False)
    rc, out, err = _run(cli, f"docker checkpoint create {name} sbx-snap --checkpoint-dir=/var/lib/sbx-checkpoints", timeout=120, check=False)
    if rc != 0:
        print("  [X] DUMP failed")
        _run(cli, f"docker rm -f {name}", check=False)
        return False
    print("  [ok] dump ok")
    rc, out, err = _run(cli, f"docker start --checkpoint sbx-snap --checkpoint-dir=/var/lib/sbx-checkpoints {name}", timeout=120, check=False)
    if rc != 0:
        print("  [X] RESTORE failed")
        if "already exists" in (out + err):
            print("    --> containerd content-store collision")
        _run(cli, f"docker rm -f {name}", check=False)
        return False
    time.sleep(2)
    rc, out, _ = _run(cli, f"docker inspect -f '{{{{.State.Running}}}}' {name}", check=False)
    running = out.strip() == "true"
    print(f"  [{'ok' if running else 'X'}] running after restore: {out.strip()}")
    _run(cli, f"docker rm -f {name}", check=False)
    return running


def main():
    print("=" * 60)
    print("finish_criu_fix.py -- continue CRIU activation")
    print("=" * 60)
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(HOST, username=USER, password=PWD, timeout=15)

    try:
        # Check which images exist
        rc, out, _ = _run(cli, "docker images --format '{{.Repository}}:{{.Tag}}' | grep sandbox/", check=False)
        existing = set(out.strip().splitlines())
        print(f"\n  existing images: {existing}")

        builds = [
            ("sandbox/base:v1", f"cd {REMOTE_DIR} && docker build -t sandbox/base:v1 -f images/base/Dockerfile .", 1800),
            ("sandbox/browser:v1", f"cd {REMOTE_DIR} && docker build -t sandbox/browser:v1 -f images/browser/Dockerfile .", 900),
            ("sandbox/all-in-one:v1", f"cd {REMOTE_DIR} && docker build -t sandbox/all-in-one:v1 -f images/all-in-one/Dockerfile .", 900),
            ("sandbox/code-interpreter:v1", f"cd {REMOTE_DIR} && docker build -t sandbox/code-interpreter:v1 -f images/code-interpreter/Dockerfile .", 900),
            ("sandbox/control-plane:v1", f"cd {REMOTE_DIR} && docker build -t sandbox/control-plane:v1 -f server/Dockerfile .", 900),
        ]
        for name, cmd, t in builds:
            if name in existing:
                print(f"\n  [skip] {name} already built")
                continue
            print(f"\n  [build] {name}...")
            _run(cli, cmd, timeout=t, check=True)

        # Compose up
        print(f"\n  compose up...")
        _run(cli, f"cd {REMOTE_DIR}/deploy && docker compose up -d --build", timeout=600, check=False)
        time.sleep(5)

        # Wait for control-plane
        print(f"\n  waiting for control-plane...")
        for i in range(30):
            rc, out, _ = _run(cli, "curl -sf http://127.0.0.1:8902/health", check=False)
            if rc == 0 and out.strip():
                print(f"  [ok] control-plane healthy: {out.strip()}")
                break
            time.sleep(2)
        else:
            print(f"  [X] control-plane didn't come up")
            _run(cli, "docker ps -a --filter name=sandbox", check=False)
            _run(cli, "docker logs sandbox-control-plane --tail 30", check=False)
            return 1

        # Register templates
        print(f"\n  registering templates...")
        rc, out, _ = _run(cli, f"curl -s http://127.0.0.1:8902/v2/templates -H 'Authorization: Bearer {API_KEYS}'", check=False)
        try:
            names = {t.get("name") for t in json.loads(out.strip() or "[]")}
        except Exception:
            names = set()
        specs = [
            {"name": "code-interpreter", "image": "sandbox/code-interpreter:v1", "cpuCount": 1, "memoryMB": 2048},
            {"name": "browser", "image": "sandbox/browser:v1", "cpuCount": 1, "memoryMB": 2048, "browserEnabled": True},
            {"name": "all-in-one", "image": "sandbox/all-in-one:v1", "cpuCount": 2, "memoryMB": 3072, "browserEnabled": True},
        ]
        for s in specs:
            if s["name"] in names:
                continue
            payload = json.dumps(json.dumps(s)).replace("$", "\\$")
            _run(cli, f"curl -s -X POST http://127.0.0.1:8902/v3/templates -H 'Authorization: Bearer {API_KEYS}' -H 'Content-Type: application/json' -d '{payload}'", check=False)
        _run(cli, f"curl -s http://127.0.0.1:8902/v2/templates -H 'Authorization: Bearer {API_KEYS}'", check=False)

        # CRIU test
        ok_criu = criu_roundtrip(cli)

    finally:
        cli.close()

    print("\n" + "=" * 60)
    if ok_criu:
        print("[PASS] CRIU fully activated")
        return 0
    else:
        print("[FAIL] CRIU round-trip failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
