"""Sync P4 第六刀 files to server, restart control-plane + edge-proxy, run session smoke."""
import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv(os.path.join(_ROOT, ".env"))

import paramiko

from deploy_server import API_KEYS, DOMAIN, HOST, PWD, USER

REMOTE = "/srv/sandbox-service"
FIRST_KEY = API_KEYS.split(",")[0].strip()
FILES = [
    "server/store.py",
    "server/main.py",
    "server/proxy.py",
    "tests/session_smoke.py",
]


def run(cli, cmd, timeout=120, check=True):
    _, o, e = cli.exec_command(cmd, timeout=timeout)
    rc = o.channel.recv_exit_status()
    out = o.read().decode("utf-8", "replace")
    err = e.read().decode("utf-8", "replace")
    print(f"\n$ {cmd}\n[rc={rc}]")
    if out.strip():
        print(out[-3000:])
    if err.strip():
        print("STDERR:", err[-1500:])
    if check and rc != 0:
        raise SystemExit(f"cmd failed: {cmd}")
    return out


def main():
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(HOST, username=USER, password=PWD, timeout=10)
    sftp = cli.open_sftp()

    for rel in FILES:
        local = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), rel.replace("/", os.sep))
        print(f"scp {local} -> {REMOTE}/{rel}")
        sftp.put(local, f"{REMOTE}/{rel}")

    # restart both containers so they pick up the new code (volume-mounted :ro)
    run(cli, "docker restart sandbox-control-plane sandbox-edge-proxy", timeout=60)
    time.sleep(4)

    # sanity: DB migration applied
    out = run(cli,
              "docker exec sandbox-control-plane python -c "
              "\"import sqlite3; c=sqlite3.connect('/data/sandbox.db'); "
              "print([r[1] for r in c.execute('PRAGMA table_info(sandboxes)')])\"")
    if "session_id" not in out:
        raise SystemExit("[!] session_id column missing after restart")
    print("[ok] sandboxes.session_id present")

    # run smoke
    py_env = " ".join([
        f"SBX_API_URL=http://127.0.0.1:8902",
        f"SBX_API_KEY={FIRST_KEY}",
        f"SBX_DOMAIN={DOMAIN}",
        f"SBX_INSECURE=1",
    ])
    rc = run(cli, f"cd {REMOTE} && {py_env} python -m tests.session_smoke",
             timeout=600, check=False)
    sftp.close()
    cli.close()
    if "ALL SESSION-SMOKE TESTS PASSED" not in (rc or ""):
        sys.exit("[!] session smoke did not report PASS — inspect output above")
    print("\nDEPLOY + SESSION SMOKE OK")


if __name__ == "__main__":
    main()
