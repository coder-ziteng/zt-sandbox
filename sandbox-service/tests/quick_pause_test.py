"""Fast iteration test: create -> run -> pause -> connect -> run -> kill."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["SSL_CERT_FILE"] = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "certs", "ca.pem")
os.environ["E2B_API_URL"] = os.environ.get("SBX_API_URL", "http://<internal-host>:8902")
os.environ["E2B_API_KEY"] = os.environ.get("SBX_E2B_KEY", "")
os.environ["NO_PROXY"] = os.environ.get("SBX_NO_PROXY", "<internal-host>,.nip.io,*.nip.io")
os.environ["no_proxy"] = os.environ["NO_PROXY"]

SBX_API_KEY = os.environ.get("SBX_API_KEY", "")
import httpx
API_HEADERS = {"Authorization": f"Bearer {SBX_API_KEY}"}
OPTS = dict(api_url=os.environ["E2B_API_URL"], api_key=os.environ["E2B_API_KEY"],
            headers=API_HEADERS, request_timeout=60, timeout=300)

resp = httpx.get("http://<internal-host>:8902/v2/templates", headers=API_HEADERS, timeout=10)
TEMPLATE = resp.json()[0]["templateCode"]

from e2b import Sandbox as E2BSandbox
sbx = E2BSandbox.create(template=TEMPLATE, **OPTS)
print("created:", sbx.sandbox_id)
r = sbx.commands.run("echo first")
print("first run:", r.stdout, r.exit_code)

_opts = {k: v for k, v in OPTS.items() if k != "timeout"}
sbx.beta_pause(**_opts)
print("paused")
print("resume:", E2BSandbox._cls_resume(sandbox_id=sbx.sandbox_id, **_opts))
sbx2 = E2BSandbox.connect(sandbox_id=sbx.sandbox_id, **OPTS)
print("connected")
r2 = sbx2.commands.run("echo after-resume")
print("after-resume run:", r2.stdout, r2.exit_code)
print("kill:", sbx2.kill())
print("DONE")
