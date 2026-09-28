import paramiko

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect("<internal-host>", username="root", password="<redacted>", timeout=10)
_, o, e = cli.exec_command("journalctl -u docker-sbx.service --no-pager -n 12 | cat", timeout=60)
print(o.read().decode("utf-8", "ignore"))
_, o2, _ = cli.exec_command(
    "timeout 15 /usr/bin/dockerd --config-file /etc/docker-sbx/daemon.json "
    "--pidfile /var/run/docker-sbx.pid --exec-root /var/run/docker-sbx-exec 2>&1 | tail -20", timeout=60)
print("--- foreground run ---")
print(o2.read().decode("utf-8", "ignore"))
cli.close()
