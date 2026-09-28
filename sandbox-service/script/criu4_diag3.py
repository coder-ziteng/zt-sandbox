import time

import paramiko

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(HOST, username=USER, password=PWD, timeout=10)
for i in range(6):
    _, o, _ = cli.exec_command("ls -l /var/run/docker-sbx.sock 2>&1; systemctl is-active docker-sbx", timeout=30)
    print(f"[{i}]", o.read().decode().strip().replace("\n", " | "))
    time.sleep(5)

_, o, _ = cli.exec_command("journalctl -u docker-sbx.service --no-pager -n 15 | cat", timeout=60)
print("\n--- journal ---")
print(o.read().decode("utf-8", "ignore"))
cli.close()
