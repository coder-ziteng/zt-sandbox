"""E2E smoke test for sandbox-service using the real e2b SDKs (run from client machine)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["SSL_CERT_FILE"] = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "certs", "ca.pem")
os.environ["E2B_API_URL"] = os.environ.get("SBX_API_URL", "http://192.168.2.162:8902")
os.environ["E2B_API_KEY"] = os.environ.get("SBX_E2B_KEY", "")
os.environ["NO_PROXY"] = os.environ.get("SBX_NO_PROXY", "192.168.2.162,.nip.io,*.nip.io")
os.environ["no_proxy"] = os.environ["NO_PROXY"]

SBX_API_KEY = os.environ.get("SBX_API_KEY", "")
API_HEADERS = {"Authorization": f"Bearer {SBX_API_KEY}"}
OPTS = dict(
    api_url=os.environ["E2B_API_URL"],
    api_key=os.environ["E2B_API_KEY"],
    headers=API_HEADERS,
    request_timeout=120,
    timeout=600,
)

import httpx

resp = httpx.get("http://192.168.2.162:8902/v2/templates", headers=API_HEADERS, timeout=10)
tpl_codes = [t.get("templateCode") for t in resp.json()]
print("templates:", tpl_codes)
TEMPLATE = os.environ.get("SBX_TEMPLATE") or (tpl_codes[0] if tpl_codes else "code-interpreter-v1")
print("using template:", TEMPLATE)


def step(name, fn):
    print(f"\n=== {name} ===")
    result = fn()
    print("OK:", result if result is not None else "")
    return result


# ---------- 1. commands + files ----------
from e2b import Sandbox as E2BSandbox

sbx = E2BSandbox.create(template=TEMPLATE, **OPTS)
print("sandbox created:", sbx.sandbox_id)

r = sbx.commands.run("echo hello-from-sandbox && python3 --version")
step("commands.run", lambda: (r.stdout, r.exit_code))

sbx.files.write("/home/user/workspace/hello.txt", "hello sandbox-service")
content = sbx.files.read("/home/user/workspace/hello.txt")
step("files.write/read", lambda: content)

sbx.files.make_dir("/home/user/workspace/sub")
entries = sbx.files.list("/home/user/workspace")
step("files.list", lambda: [(e.name, e.type.value if e.type else None) for e in entries])

info = sbx.get_info()
step("get_info", lambda: (info.sandbox_id, info.state, info.cpu_count, info.memory_mb))

# ---------- 2. run_code ----------
from e2b_code_interpreter import Sandbox as CISandbox

ci = CISandbox.create(template=TEMPLATE, **OPTS)
execution = ci.run_code("print(1 + 1)\nimport sys\nprint(sys.version.split()[0])", timeout=120)
step("run_code", lambda: (execution.text, execution.logs.stdout, execution.error))

execution2 = ci.run_code("x = 41\nx + 1")
step("run_code stateful", lambda: (execution2.text, execution2.results and execution2.results[0].text))

err_exec = ci.run_code("1 / 0", timeout=60)
step("run_code error", lambda: (err_exec.error.name, err_exec.error.value) if err_exec.error else "no error?!")

ci.kill()

# ---------- 3. lifecycle ----------
sbx.beta_pause(**{k: v for k, v in OPTS.items() if k != "timeout"})
step("pause", lambda: "beta_pause ok")

resumed = E2BSandbox._cls_resume(sandbox_id=sbx.sandbox_id, **{k: v for k, v in OPTS.items() if k != "timeout"})
step("resume", lambda: resumed)

sbx2 = E2BSandbox.connect(sandbox_id=sbx.sandbox_id, **OPTS)
r2 = sbx2.commands.run("echo after-resume")
step("connect after pause + run", lambda: (r2.stdout, r2.exit_code))

killed = sbx2.kill()
step("kill", lambda: killed)

print("\nALL SMOKE TESTS PASSED")
