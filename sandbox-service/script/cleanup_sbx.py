"""清理 162 上残留的沙箱实例（释放内存配额）。"""
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
        print(out)
    if err:
        print("[err]", err[:300])
    print()
    return out


run("curl -s -m 8 -H f'X-API-KEY: {E2B_KEY}' http://127.0.0.1:8902/v2/sandboxes | head -c 2000")
out = run("docker ps --filter name=sbx- --format '{{.Names}}'")
names = [n for n in out.splitlines() if n.strip().startswith("sbx-")]
for n in names:
    run(f"docker rm -f {n}")
run("curl -s -m 5 http://127.0.0.1:8902/health")
cli.close()
