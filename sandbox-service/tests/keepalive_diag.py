"""Diagnostic: trace what actually happens to a paused sandbox's host port."""
import os
import subprocess
import time

import httpx

API_KEY = os.environ["API_KEY"]
HOST = "127.0.0.1"
PORT = 8902
H = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}


def shell(cmd):
    return subprocess.check_output(cmd, shell=True, text=True)


# Get first template
templates = httpx.get(f"http://{HOST}:{PORT}/v2/templates", headers=H).json()
tpl_code = templates[0]["templateCode"]
print(f"using template {tpl_code}")

# Create sandbox
r = httpx.post(f"http://{HOST}:{PORT}/sandboxes", headers=H,
               json={"templateID": tpl_code, "timeout": 120})
sid = r.json()["sandboxID"]
print(f"created {sid}")
time.sleep(1)

# Get state and host port via API + direct DB read (control plane uses
# /data/sandbox.db; on the host that's /srv/sandbox-service/data/sandbox.db).
state = httpx.get(f"http://{HOST}:{PORT}/sandboxes/{sid}", headers=H).json()
print(f"  state={state['state']}, pause_mode={state.get('pauseMode')}")
host_port = subprocess.check_output(
    "python3 -c \"import sqlite3; c=sqlite3.connect('/srv/sandbox-service/data/sandbox.db'); print(c.execute('SELECT host_port_envd FROM sandboxes WHERE sandbox_id=?', ('" + sid + "',)).fetchone()[0])\"",
    shell=True, text=True).strip()
print(f"  envd host port={host_port}")

# Listener before pause
out = subprocess.run(f"ss -tlnp | grep :{host_port} || echo NOT-LISTENING",
                     shell=True, capture_output=True, text=True)
print(f"  listener before pause: {out.stdout.strip()}")

# Pause
pause_body = '{"criu":"auto"}'
r = httpx.post(f"http://{HOST}:{PORT}/sandboxes/{sid}/pause", headers=H, json={"criu": "auto"})
print(f"  pause rc={r.status_code}")

time.sleep(1)

# After pause
state = httpx.get(f"http://{HOST}:{PORT}/sandboxes/{sid}", headers=H).json()
print(f"  state after pause={state['state']}")
out = subprocess.run(
    f"docker ps -a --filter 'name=sbx-{sid}' --format '{{{{.Status}}}}'",
    shell=True, capture_output=True, text=True)
print(f"  docker: {out.stdout.strip()}")
out = subprocess.run(f"ss -tlnp | grep :{host_port} || echo NOT-LISTENING",
                     shell=True, capture_output=True, text=True)
print(f"  listener after pause: {out.stdout.strip()}")

# Try direct connection to the host port (bypass edge proxy)
try:
    r = httpx.get(f"http://127.0.0.1:{host_port}/health", timeout=3)
    print(f"  direct conn to host port: {r.status_code}")
except Exception as e:
    print(f"  direct conn to host port: FAILED ({type(e).__name__}: {e})")

# Try via edge proxy
try:
    r = httpx.get(f"https://192.168.2.162/health",
                  headers={"Host": f"49983-{sid}.192.168.2.162.nip.io"},
                  timeout=10, verify=False)
    print(f"  edge proxy conn: {r.status_code} ({r.elapsed.total_seconds():.2f}s)")
except Exception as e:
    print(f"  edge proxy conn: FAILED ({type(e).__name__}: {e})")

# Cleanup
httpx.delete(f"http://{HOST}:{PORT}/sandboxes/{sid}", headers=H)
print("cleaned up")