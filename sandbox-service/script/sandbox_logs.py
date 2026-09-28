import paramiko, sys
cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(HOST, username=USER, password=PWD, timeout=10)
sid = sys.argv[1] if len(sys.argv) > 1 else "sbx953636afc89a439eb"
_, stdout, _ = cli.exec_command(f"docker logs sbx-{sid} 2>&1 | tail -40", timeout=30)
print(stdout.read().decode())
cli.close()
