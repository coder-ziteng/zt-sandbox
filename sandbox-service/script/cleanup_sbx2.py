"""通过控制面 API 删除残留沙箱登记，核对配额归零。"""
import paramiko

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect("<internal-host>", username="root", password="<redacted>", timeout=10)


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


for sid in ["sbx5e15694fca6a4c5cb", "sbxff0c2dc1540e422da"]:
    run(f"curl -s -m 8 -X DELETE -H 'X-API-KEY: <e2b-key-redacted>' http://127.0.0.1:8902/sandboxes/{sid} -w '\\ncode=%{{http_code}}\\n'")
run("curl -s -m 5 http://127.0.0.1:8902/health")
cli.close()
