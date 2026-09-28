import time

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


time.sleep(8)  # 等控制面完全就绪
run("curl -s -m 5 -w '\\nhealth_code=%{http_code}\\n' http://127.0.0.1:8902/health")
run("curl -s -m 8 -H f'X-API-KEY: {E2B_KEY}' http://127.0.0.1:8902/sandboxes -w '\\nlist_code=%{http_code}\\n' | head -20")
run("curl -s -m 5 -o /dev/null -w 'proxy443_code=%{http_code}\\n' http://127.0.0.1:443/")
run("ss -ltnp | grep -E ':8902|:443' | head -5")
run("docker logs sandbox-control-plane --tail 5 2>&1")
run("docker logs sandbox-edge-proxy --tail 3 2>&1")
run("docker network ls | grep -i sbx")
run("iptables -L DOCKER-USER -n 2>/dev/null | head -8")
cli.close()
