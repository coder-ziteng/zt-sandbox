import time

import paramiko

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect("<internal-host>", username="root", password="<redacted>", timeout=10)
for i in range(8):
    _, o, _ = cli.exec_command(
        "systemctl is-active docker; ls -l /var/run/docker.sock 2>&1 | tail -1; "
        "docker ps --format '{{.Names}}' 2>&1 | head -5", timeout=40)
    print(f"[{i}]", o.read().decode("utf-8", "ignore").strip().replace("\n", " | "))
    time.sleep(8)
_, o, _ = cli.exec_command("journalctl -u docker.service --no-pager -n 20 | cat", timeout=60)
print("\n--- docker.service journal ---")
print(o.read().decode("utf-8", "ignore"))
cli.close()
