"""手动复现：create -> pause -> resume -> 带 token 直连 envd /process 验证鉴权。"""
import json
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
    print("$", cmd[:160])
    if out:
        print(out[:1200])
    if err:
        print("[err]", err[:300])
    print()
    return out


H = "-H 'Content-Type: application/json' -H f'X-API-KEY: {E2B_KEY}'"
# 1. create
out = run(f"curl -s -m 30 -X POST {H} http://127.0.0.1:8902/sandboxes -d '{{\"templateID\":\"tmplc5dbfe8c710542378\",\"timeout\":600}}'")
sb = None
for line in out.splitlines():
    line = line.strip()
    if line.startswith("{"):
        sb = json.loads(line)
        break
sid = sb["sandboxID"]
token = sb.get("envdAccessToken")
print("sandbox:", sid, "token:", (token or "")[:8], "...\n")
time.sleep(6)

# 2. 恢复前直接 envd 验证（经 host_port_envd）
row = run(f"docker inspect -f '{{{{json .NetworkSettings.Ports}}}}' sbx-{sid}")
port = run(f"docker port sbx-{sid} 49983/tcp | head -1 | awk -F: '{{print $2}}'").splitlines()[-1].strip()
print("envd host port:", port, "\n")
run(f"curl -s -m 10 -X POST http://127.0.0.1:{port}/process.Process/Start -H 'Content-Type: application/json' -H 'X-Access-Token: {token}' -d '{{\"process\":{{\"cmd\":\"echo hi-pre-pause\"}}}}' | head -c 300")

# 3. pause
run(f"curl -s -m 30 -X POST {H} http://127.0.0.1:8902/sandboxes/{sid}/pause -d '{{}}' -o /dev/null -w 'pause=%{{http_code}}\\n'")
# 4. resume
run(f"curl -s -m 60 -X POST {H} http://127.0.0.1:8902/sandboxes/{sid}/resume -d '{{\"timeout\":600}}' -o /dev/null -w 'resume=%{{http_code}}\\n'")
time.sleep(3)
# 5. resume 后再直连 envd
run(f"curl -s -m 10 -X POST http://127.0.0.1:{port}/process.Process/Start -H 'Content-Type: application/json' -H 'X-Access-Token: {token}' -d '{{\"process\":{{\"cmd\":\"echo hi-post-resume\"}}}}' | head -c 300")
# 对比容器内实际 ENVD_TOKEN
run(f"docker exec sbx-{sid} printenv ENVD_TOKEN | head -c 12")
print("db token  :", (token or "")[:12])

# 6. cleanup
run(f"curl -s -m 15 -X DELETE {H} http://127.0.0.1:8902/sandboxes/{sid} -o /dev/null -w 'kill=%{{http_code}}\\n'")
cli.close()
