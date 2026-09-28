import paramiko, sys
cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect("<internal-host>", username="root", password="<redacted>", timeout=10)
sid = sys.argv[1] if len(sys.argv) > 1 else "sbxb9ccaf808d034a9d9"
_, stdout, _ = cli.exec_command(f"docker logs sbx-{sid} 2>&1 | grep -A2 'start]' | tail -12", timeout=30)
print(stdout.read().decode())
_, stdout, _ = cli.exec_command(f"docker logs sbx-{sid} 2>&1 | tail -8", timeout=30)
print(stdout.read().decode())
cli.close()
