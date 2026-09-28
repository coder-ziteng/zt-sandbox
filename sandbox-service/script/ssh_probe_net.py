import paramiko, json

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect("<internal-host>", username="root", password="<redacted>", timeout=10)

CMDS = [
    ("baidu", "curl -s -o /dev/null -w '%{http_code}' --connect-timeout 5 https://www.baidu.com || echo X"),
    ("pypi-tuna", "curl -s -o /dev/null -w '%{http_code}' --connect-timeout 5 https://pypi.tuna.tsinghua.edu.cn/simple/ || echo X"),
    ("daocloud-docker", "curl -s -o /dev/null -w '%{http_code}' --connect-timeout 5 https://m.daocloud.io/v2/ || echo X"),
    ("daocloud-ghcr", "curl -s -o /dev/null -w '%{http_code}' --connect-timeout 5 https://ghcr.daocloud.io/v2/ || echo X"),
    ("daocloud-gcr", "curl -s -o /dev/null -w '%{http_code}' --connect-timeout 5 https://gcr.daocloud.io/v2/ || echo X"),
    ("docker-1ms", "curl -s -o /dev/null -w '%{http_code}' --connect-timeout 5 https://docker.1ms.run/v2/ || echo X"),
    ("dockerhub-proxy-xuanyuan", "curl -s -o /dev/null -w '%{http_code}' --connect-timeout 5 https://docker.xuanyuan.me/v2/ || echo X"),
    ("gh-proxy", "curl -s -o /dev/null -w '%{http_code}' --connect-timeout 5 https://gh-proxy.com || echo X"),
    ("ghproxy-net", "curl -s -o /dev/null -w '%{http_code}' --connect-timeout 5 https://ghproxy.net || echo X"),
    ("moocss", "curl -s -o /dev/null -w '%{http_code}' --connect-timeout 5 https://github.moeyy.xyz || echo X"),
    ("hub-e2bdev", "curl -s -o /dev/null -w '%{http_code}' --connect-timeout 8 'https://m.daocloud.io/v2/e2bdev/code-interpreter/tags/list' || echo X"),
    ("hub-e2b-manifest", "curl -s --connect-timeout 8 'https://m.daocloud.io/v2/e2bdev/envd/tags/list' | head -c 300 || echo X"),
    ("dns", "getent hosts m.daocloud.io | head -1"),
]
out = {}
for k, c in CMDS:
    _, o, e = cli.exec_command(c, timeout=25)
    b = o.read().decode().strip(); err = e.read().decode().strip()
    out[k] = b if b else ("ERR:" + err[:80] if err else "empty")
cli.close()
print(json.dumps(out, ensure_ascii=False, indent=1))
