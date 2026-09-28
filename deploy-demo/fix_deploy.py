import paramiko
import sys
import time

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect('<internal-host>', username='root', password='<redacted>', timeout=10)

def exec_cmd(cmd):
    stdin, stdout, stderr = client.exec_command(cmd)
    return stdout.read().decode('utf-8', errors='replace'), stderr.read().decode('utf-8', errors='replace')

# Check vue-frontend structure
stdout, _ = exec_cmd('ls -la /home/law_database/vue-frontend/')
print('=== vue-frontend ===')
sys.stdout.buffer.write(stdout.encode('utf-8'))

# Check if dist exists
stdout, _ = exec_cmd('ls -la /home/law_database/vue-frontend/dist/ 2>/dev/null || echo "dist not found"')
print('=== dist ===')
sys.stdout.buffer.write(stdout.encode('utf-8'))

# Check python processes
stdout, _ = exec_cmd('ps aux | grep python | grep -v grep')
print('=== python processes ===')
sys.stdout.buffer.write(stdout.encode('utf-8'))

# Kill existing python processes
exec_cmd('pkill -9 -f "python3 run.py" || true')
exec_cmd('sleep 1')

# Start backend
stdout, _ = exec_cmd('cd /home/law_database && nohup python3 run.py > app.log 2>&1 &')
print('=== starting backend ===')
sys.stdout.buffer.write(stdout.encode('utf-8'))

time.sleep(3)

# Test backend
stdout, _ = exec_cmd('curl -s http://localhost:9000/docs | head -3')
print('=== backend test ===')
sys.stdout.buffer.write(stdout.encode('utf-8'))

client.close()
