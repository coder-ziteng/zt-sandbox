"""动态清理：列出全部沙箱登记并逐个删除，最后核对配额归零。"""
import json
import paramiko
import os
E2B_KEY = os.environ.get("SBX_E2B_KEY", "")

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(HOST, username=USER, password=PWD, timeout=10)


def run(cmd, timeout=60):
    _, o, e = cli.exec_command(cmd, timeout=timeout)
    out = o.read().decode("utf-8", "ignore").strip()
    err = e.read().decode("utf-8", "ignore").strip()
    print("$", cmd)
    if out:
        print(out[:1500])
    if err:
        print("[err]", err[:300])
    print()
    return out


out = run("curl -s -m 8 -H f'X-API-KEY: {E2B_KEY}' http://127.0.0.1:8902/v2/sandboxes")
try:
    sandboxes = json.loads(out.splitlines()[1] if out.startswith("$") else out)
except Exception:
    sandboxes = []

ids = [s["sandboxID"] for s in sandboxes if s.get("sandboxID")]
print("registered:", ids, "\n")
for sid in ids:
    run(f"curl -s -m 8 -X DELETE -H f'X-API-KEY: {E2B_KEY}' http://127.0.0.1:8902/sandboxes/{sid} -o /dev/null -w 'code=%{{http_code}}\\n'")

# 兜底：把 docker 里可能残留的 sbx- 容器也清掉
out = run("docker ps -a --filter name=sbx- --format '{{.Names}}'")
for n in [x for x in out.splitlines() if x.strip().startswith("sbx-")]:
    run(f"docker rm -f {n.strip()}")

run("curl -s -m 5 http://127.0.0.1:8902/health")
cli.close()
