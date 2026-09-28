"""Verify API_KEYS actually reached the control-plane container."""
import paramiko, json

HOST, USER, PWD = "<internal-host>", "root", "123456"
CMD = r"""
cat /srv/sandbox-service/deploy/.env
echo "---"
docker exec sandbox-control-plane env | grep API_KEYS
echo "--- direct auth test"
curl -s -o /dev/null -w 'sk-key:%{http_code}\n' http://127.0.0.1:8902/v2/templates -H 'Authorization: Bearer <dev-key-redacted>'
curl -s -o /dev/null -w 'e2b-key:%{http_code}\n' http://127.0.0.1:8902/v2/templates -H 'Authorization: Bearer <e2b-key-redacted>'
"""

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(HOST, username=USER, password=PWD, timeout=15)
_, o, e = cli.exec_command(CMD, timeout=60)
out = o.read().decode("utf-8", "replace")
cli.close()
print(out)
