import os
import paramiko
import sys

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(
    os.environ.get("REMOTE_HOST") or sys.exit("[ERROR] REMOTE_HOST is required"),
    username=os.environ.get("REMOTE_USER") or sys.exit("[ERROR] REMOTE_USER is required"),
    password=os.environ.get("REMOTE_PASSWORD") or sys.exit("[ERROR] REMOTE_PASSWORD is required"),
    timeout=10,
)

def exec_cmd(cmd):
    stdin, stdout, stderr = client.exec_command(cmd)
    return stdout.read().decode('utf-8', errors='replace'), stderr.read().decode('utf-8', errors='replace')

# Check directory structure
stdout, _ = exec_cmd('ls -la /home/law_database/')
print('=== /home/law_database ===')
sys.stdout.buffer.write(stdout.encode('utf-8'))

# Check if src directory exists
stdout, _ = exec_cmd('ls /home/law_database/vue-frontend/src/ 2>/dev/null || echo "src not found (good)"')
print('=== vue-frontend/src ===')
sys.stdout.buffer.write(stdout.encode('utf-8'))

# Check dist contents
stdout, _ = exec_cmd('ls -la /home/law_database/vue-frontend/dist/')
print('=== vue-frontend/dist ===')
sys.stdout.buffer.write(stdout.encode('utf-8'))

client.close()
