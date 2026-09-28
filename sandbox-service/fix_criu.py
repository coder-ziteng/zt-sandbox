"""fix_criu.py -- activate real CRIU (stable route).

Root cause: Docker 29.5.2 runs with `io.containerd.snapshotter.v1` (containerd image
store). In that mode `docker checkpoint create` dumps successfully, but `docker start
--checkpoint` fails with "failed to upload checkpoint to containerd: ... already exists"
because checkpoint upload collides with containerd's content store.

Fix route:
  1. Write daemon.json with features.containerd-snapshotter=false
  2. Restart dockerd, VERIFY the setting took effect
  3. Full prune + rebuild
  4. Re-register templates
  5. Real dump+restore round-trip self-test

Usage:
    python fix_criu.py
"""
import io
import json
import os
import sys
import tarfile
import time

import paramiko

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from deploy_server import (
    HOST, USER, PWD, REMOTE_DIR, API_KEYS, DOMAIN, build_tar,
)

# Force UTF-8 output on Windows consoles (avoids GBK encode errors on emoji/unicode).
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

TIMEOUT_CONNECT = 15
TIMEOUT_DOCKER_UP = 90


# ---------------------------------------------------------------------------
# SSH helpers
# ---------------------------------------------------------------------------

def _run(cli, cmd, timeout=600, check=True, label=None):
    """Run command over SSH, print output, return (rc, stdout, stderr)."""
    tag = f"  [{label}]" if label else ""
    print(f"\n  $ {cmd}{tag}")
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
            raise SystemExit(f"command failed (rc={rc}): {cmd}")
        return rc, out, err
    except SystemExit:
        raise
    except Exception as e:
        if not check:
            print(f"    [error] {e}")
            return -1, "", str(e)
        raise


def _ssh():
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(HOST, username=USER, password=PWD, timeout=TIMEOUT_CONNECT)
    return cli


# ---------------------------------------------------------------------------
# Server state probes
# ---------------------------------------------------------------------------

def server_state(cli):
    """Return (driver_name, snapshotter_enabled_bool).

    Detection: `docker info` prints `Storage Driver: overlayfs` and, when the
    containerd image store is enabled, a separate line `driver-type: io.containerd.snapshotter.v1`.
    We flag snapshotter=true iff that driver-type line names containerd.
    """
    rc, out, _ = _run(cli, "docker info 2>/dev/null | grep -iE 'storage driver|driver-type|snapshotter'", check=False)
    driver = ""
    snap = False
    for line in out.splitlines():
        ll = line.strip().lower()
        if ll.startswith("storage driver:"):
            driver = line.split(":", 1)[1].strip()
        if "driver-type:" in ll and "containerd" in ll:
            snap = True
        # journal-style line (just in case it leaks into docker info on some versions)
        if "containerd snapshotter integration enabled" in ll:
            snap = True
    return driver, snap


def daemon_json_contents(cli):
    rc, out, _ = _run(cli, "cat /etc/docker/daemon.json", check=False)
    return out.strip()


# ---------------------------------------------------------------------------
# CRIU round-trip self-test (mirrors server/runtime._criu_probe logic)
# ---------------------------------------------------------------------------

def criu_roundtrip(cli):
    """Dump + restore a throwaway alpine container; return True iff both succeed."""
    print("\n" + "=" * 60)
    print("  CRIU round-trip self-test")
    print("=" * 60)
    img = "m.daocloud.io/docker.io/library/alpine:3.20"
    name = "sbx-criu-verify"

    _run(cli, f"docker rm -f {name} 2>/dev/null", check=False)

    rc, out, _ = _run(cli, f"docker pull {img}", timeout=180, check=False)
    if rc != 0:
        print(f"  ! pull failed, using whatever is available")

    rc, _, _ = _run(cli, f"docker run -d --name {name} {img} sleep 60", check=False)
    if rc != 0:
        print(f"  [X] could not start test container")
        return False

    time.sleep(2)

    # ---- dump ----
    _run(cli, f"docker checkpoint rm {name} sbx-snap 2>/dev/null", check=False)
    rc, out, err = _run(
        cli,
        f"docker checkpoint create {name} sbx-snap --checkpoint-dir=/var/lib/sbx-checkpoints",
        timeout=120, check=False,
    )
    if rc != 0:
        print(f"  [X] DUMP failed")
        _run(cli, f"docker rm -f {name}", check=False)
        return False
    print(f"  [ok] dump ok")

    # ---- restore ----
    rc, out, err = _run(
        cli,
        f"docker start --checkpoint sbx-snap --checkpoint-dir=/var/lib/sbx-checkpoints {name}",
        timeout=120, check=False,
    )
    if rc != 0:
        print(f"  [X] RESTORE failed")
        if "already exists" in (out + err):
            print(f"    --> still hitting the containerd content-store collision")
        _run(cli, f"docker rm -f {name}", check=False)
        return False

    time.sleep(2)
    rc, out, _ = _run(cli, f"docker inspect -f '{{{{.State.Running}}}}' {name}", check=False)
    running = out.strip() == "true"
    print(f"  [{'ok' if running else 'X'}] container running after restore: {out.strip()}")

    _run(cli, f"docker rm -f {name}", check=False)
    return running


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("fix_criu.py -- activate real CRIU")
    print("=" * 60)
    print(f"target: {HOST}")
    print(f"route : disable containerd-snapshotter -> full wipe/rebuild")
    print()

    cli = _ssh()
    ok_docker = False
    ok_criu = False

    try:
        # ---- before ----
        driver, snap = server_state(cli)
        print(f"  current driver    : {driver}")
        print(f"  containerd snap   : {snap}")
        print(f"  daemon.json       : {daemon_json_contents(cli)}")

        if not snap:
            # snapshotter already off -- but we still need to rebuild if images are missing
            rc_img, out_img, _ = _run(cli, "docker images -q sandbox/base:v1", check=False)
            images_present = bool(out_img.strip())
            if images_present:
                print(f"\n  [ok] containerd-snapshotter already disabled and images present -- running probe only")
                ok_criu = criu_roundtrip(cli)
                print(f"\n  {'[PASS] CRIU available' if ok_criu else '[FAIL] CRIU still unavailable'}")
                return 0 if ok_criu else 1
            print(f"\n  [note] snapshotter already off but images missing -- proceeding with full rebuild")

        # ---- 1. backup daemon.json ----
        _run(cli, r"cp /etc/docker/daemon.json /etc/docker/daemon.json.bak.$(date +%s) 2>/dev/null", check=False)

        # ---- 2. write new daemon.json ----
        new_cfg = {"experimental": True, "features": {"containerd-snapshotter": False}}
        new_json = json.dumps(new_cfg, indent=2)
        print(f"\n  new daemon.json:")
        for line in new_json.splitlines():
            print(f"    {line}")

        escaped = new_json.replace("\\", "\\\\").replace("'", "'\\''").replace("$", "\\$").replace("`", "\\`")
        _run(cli, f"echo '{escaped}' > /tmp/daemon.json.new", check=True)
        _run(cli, "cat /tmp/daemon.json.new | python3 -c 'import sys,json; json.load(sys.stdin); print(\"json ok\")'", check=True)
        _run(cli, "mv /tmp/daemon.json.new /etc/docker/daemon.json && cat /etc/docker/daemon.json", check=True)

        # ---- 3. restart docker ----
        print(f"\n  restarting dockerd...")
        _run(cli, "systemctl restart docker", timeout=120, check=False)

        # ---- 4. verify docker is back + snapshotter gone ----
        for attempt in range(TIMEOUT_DOCKER_UP // 3):
            time.sleep(3)
            rc, out, _ = _run(cli, "docker info --format '{{.Driver}}' 2>/dev/null", check=False)
            if rc == 0 and out.strip():
                break
            print(f"    waiting for dockerd... (attempt {attempt + 1})")
        else:
            print(f"\n  [X] dockerd didn't come back up in {TIMEOUT_DOCKER_UP}s")
            print(f"    reverting daemon.json from backup...")
            _run(cli, r"ls -t /etc/docker/daemon.json.bak.* | head -1 | xargs -I {} cp {} /etc/docker/daemon.json", check=False)
            _run(cli, "systemctl restart docker", timeout=120, check=False)
            return 1

        driver2, snap2 = server_state(cli)
        print(f"  after restart:")
        print(f"    driver          : {driver2}")
        print(f"    containerd snap : {snap2}")

        if snap2:
            print(f"\n  [X] containerd-snapshotter still enabled! daemon.json may have been overridden.")
            print(f"    daemon.json now: {daemon_json_contents(cli)}")
            print(f"    docker journal tail:")
            _run(cli, "journalctl -u docker --no-pager -n 20 | tail -20", check=False)
            return 1
        print(f"  [ok] containerd-snapshotter successfully disabled")
        ok_docker = True

        # ---- 5. clean slate ----
        print(f"\n  wiping old containers/images (old containerd image-store content is invisible now)...")
        _run(cli, "docker stop $(docker ps -aq) 2>/dev/null", check=False)
        _run(cli, "docker rm -f $(docker ps -aq) 2>/dev/null", check=False)
        _run(cli, "docker system prune --all --volumes -f", timeout=600, check=False)

        # ---- 6. upload code ----
        print(f"\n  uploading source tree...")
        tar_bytes = build_tar()
        print(f"    tar size: {len(tar_bytes) / 1024 / 1024:.1f} MB")
        sftp = cli.open_sftp()
        with sftp.file("/tmp/sbx-upload.tar.gz", "wb") as f:
            f.write(tar_bytes)
        _run(cli, f"rm -rf {REMOTE_DIR} && mkdir -p {REMOTE_DIR}")
        _run(cli, f"cd /srv && tar xzf /tmp/sbx-upload.tar.gz && ls sandbox-service")
        _run(cli, f"mkdir -p {REMOTE_DIR}/certs {REMOTE_DIR}/data /var/lib/sbx-checkpoints", check=False)
        sftp.close()

        # ---- 7. rebuild all images ----
        print(f"\n  rebuilding images (this will take 10-30 min)...")
        builds = [
            (f"cd {REMOTE_DIR} && docker build -t sandbox/base:v1         -f images/base/Dockerfile .", 1800),
            (f"cd {REMOTE_DIR} && docker build -t sandbox/browser:v1      -f images/browser/Dockerfile .", 900),
            (f"cd {REMOTE_DIR} && docker build -t sandbox/all-in-one:v1   -f images/all-in-one/Dockerfile .", 900),
            (f"cd {REMOTE_DIR} && docker build -t sandbox/code-interpreter:v1 -f images/code-interpreter/Dockerfile .", 900),
            (f"cd {REMOTE_DIR} && docker build -t sandbox/control-plane:v1 -f server/Dockerfile .", 900),
        ]
        for cmd, t in builds:
            _run(cli, cmd, timeout=t, check=True)

        # ---- 8. compose up ----
        _run(cli, f"cd {REMOTE_DIR}/deploy && docker compose up -d --build", timeout=600, check=False)
        time.sleep(5)

        # ---- 9. wait for control-plane ----
        for i in range(30):
            rc, out, _ = _run(cli, "curl -sf http://127.0.0.1:8902/health", check=False)
            if rc == 0 and out.strip():
                print(f"  [ok] control-plane healthy: {out.strip()}")
                break
            time.sleep(2)
        else:
            print(f"  [X] control-plane didn't come up")
            _run(cli, "docker ps -a --filter name=sandbox --format 'table {{.Names}}\\t{{.Status}}'", check=False)
            _run(cli, "docker logs sandbox-control-plane --tail 30", check=False)
            return 1

        # ---- 10. re-register templates ----
        print(f"\n  registering templates...")
        rc, out, _ = _run(cli, f"curl -s http://127.0.0.1:8902/v2/templates -H 'Authorization: Bearer {API_KEYS}'", check=False)
        try:
            names = {t.get("name") for t in json.loads(out.strip() or "[]")}
        except Exception:
            names = set()
        specs = [
            {"name": "code-interpreter", "image": "sandbox/code-interpreter:v1", "cpuCount": 1, "memoryMB": 2048},
            {"name": "browser",          "image": "sandbox/browser:v1",         "cpuCount": 1, "memoryMB": 2048, "browserEnabled": True},
            {"name": "all-in-one",       "image": "sandbox/all-in-one:v1",      "cpuCount": 2, "memoryMB": 3072, "browserEnabled": True},
        ]
        for s in specs:
            if s["name"] in names:
                continue
            payload = json.dumps(json.dumps(s)).replace("$", "\\$")
            _run(cli,
                 f"curl -s -X POST http://127.0.0.1:8902/v3/templates "
                 f"-H 'Authorization: Bearer {API_KEYS}' -H 'Content-Type: application/json' "
                 f"-d '{payload}'",
                 check=False)
        _run(cli, f"curl -s http://127.0.0.1:8902/v2/templates -H 'Authorization: Bearer {API_KEYS}'", check=False)

        # ---- 11. CRIU round-trip self-test ----
        ok_criu = criu_roundtrip(cli)

    except SystemExit as e:
        print(f"\n  [X] aborted: {e}")
        return 2
    except Exception:
        import traceback
        traceback.print_exc()
        return 2
    finally:
        cli.close()

    print("\n" + "=" * 60)
    if ok_docker and ok_criu:
        print("[PASS] CRIU fully activated")
        print("  /health should report criu: true")
        print("  pauseMode should be 'criu' (not 'stop')")
        print("  in-memory state survives pause/resume")
        print("=" * 60)
        return 0
    elif ok_docker:
        print("[WARN] Docker switched to classic driver but CRIU round-trip still failed")
        print("  check CRIU's own error log for the real cause")
        print("=" * 60)
        return 1
    else:
        print("[FAIL] Docker daemon config switch failed; tried to revert from backup")
        print("=" * 60)
        return 1


if __name__ == "__main__":
    sys.exit(main())
