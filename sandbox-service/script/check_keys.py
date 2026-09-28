"""Verify API_KEYS actually reached the control-plane container."""
import paramiko

from deploy_server import API_KEYS, HOST, PWD, USER

# Pick the first key from the comma-separated list to test against.
_SK_KEY = API_KEYS.split(",")[0].strip()
CMD = f"""
cat /srv/sandbox-service/deploy/.env
echo "---"
docker exec sandbox-control-plane env | grep API_KEYS
echo "--- direct auth test"
curl -s -o /dev/null -w 'sk-key:%{{http_code}}\\n' http://127.0.0.1:8902/v2/templates -H 'Authorization: Bearer {_SK_KEY}'
"""

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(HOST, username=USER, password=PWD, timeout=15)
_, o, e = cli.exec_command(CMD, timeout=60)
out = o.read().decode("utf-8", "replace")
cli.close()
print(out)
