"""Inspect a live sandbox record + its published ports + in-container processes."""
import paramiko, sys
import os
SBX_API_KEY = os.environ.get("SBX_API_KEY", "")

KEY = os.environ.get("SBX_API_KEY", "")
CMD = r"""
echo "== ports"; docker ps -a --format '{{.Names}} :: {{.Ports}}' | grep -E '^sbx-' | head -5
echo "== sandbox json"; curl -s -m 5 http://127.0.0.1:8902/v2/sandboxes -H 'Authorization: Bearer %s' | head -c 1200; echo
echo "== health json"; sid=$(curl -s -m 5 http://127.0.0.1:8902/v2/sandboxes -H 'Authorization: Bearer %s' | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0]['sandboxID'] if d else '')"); echo "sid=$sid"; curl -s -m 8 "http://127.0.0.1:8902/sandboxes/$sid/health" -H 'Authorization: Bearer %s' | head -c 900; echo
echo "== container procs"; n=$(docker ps -a --format '{{.Names}}' | grep -E '^sbx-' | head -1); docker exec "$n" ps aux 2>&1 | head -12
echo "== listening"; docker exec "$n" sh -c 'netstat -tlnp 2>/dev/null | head -10 || ss -tlnp | head -10' 2>&1 | head -12
""" % (KEY, KEY, KEY)

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(HOST, username=USER, password=PWD, timeout=15)
_, o, e = cli.exec_command(CMD, timeout=120)
out = o.read().decode("utf-8", "replace")
cli.close()
print(out)
