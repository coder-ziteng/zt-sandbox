"""Regression: run P4 第四/第五刀 smokes against current server build."""
import os
import sys

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
# Pull the admin token already stored on the server's deploy/.env
ADMIN_CMD = "grep '^ADMIN_TOKEN=' /srv/sandbox-service/deploy/.env | cut -d= -f2"


def run(cli, cmd, timeout=600, check=True):
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

    admin = run(cli, ADMIN_CMD, check=False).strip()
    if not admin:
        raise SystemExit("[!] no ADMIN_TOKEN in /srv/sandbox-service/deploy/.env")
    print(f"[admin] token from .env: {admin[:8]}…")

    env = (f"SBX_API_URL=http://127.0.0.1:8902 SBX_API_KEY={FIRST_KEY} "
           f"SBX_DOMAIN={DOMAIN} SBX_INSECURE=1 SBX_ADMIN_TOKEN={admin}")

    for mod, name in [("tests.path_sandbox_smoke", "P4-4 path-sandbox"),
                      ("tests.admin_keys_smoke", "P4-5 admin-keys")]:
        out = run(cli, f"cd {REMOTE} && {env} python -m {mod}", check=False)
        if "PASSED" not in (out or ""):
            raise SystemExit(f"[!] regression failed in {name}")
        print(f"[ok] {name} still green")

    cli.close()
    print("\nREGRESSION OK")


if __name__ == "__main__":
    main()
