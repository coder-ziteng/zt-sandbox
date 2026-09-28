"""探测哪个 e2b 版本有 Sandbox.create 且支持 api_url/api_headers 参数。"""
import subprocess
import sys

PY = sys.executable
CANDIDATES = ["1.0.5", "1.3.0", "1.5.2", "1.11.1", "2.0.0", "2.1.0", "2.2.0", "2.5.0"]

CHECK = r"""
import inspect, json
from e2b import Sandbox
has_create = hasattr(Sandbox, "create")
sig = ""
path = ""
if has_create:
    sig = str(inspect.signature(Sandbox.create))
    try:
        import e2b.sandbox_sync.sandbox_api as api
        import importlib, pkgutil
        import e2b.api.client.api.sandboxes as sb
        mods = [m.name for m in pkgutil.iter_modules(sb.__path__)] if hasattr(sb, "__path__") else dir(sb)
        path = ",".join([m for m in mods if "post" in m]) if isinstance(mods, list) else ""
    except Exception as e:
        path = "err:" + str(e)
print(json.dumps({"has_create": has_create, "sig": sig, "posts": path}))
"""

for v in CANDIDATES:
    r = subprocess.run([PY, "-m", "pip", "install", "-q", f"e2b=={v}"],
                       capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        print(v, "INSTALL_FAIL", r.stderr.strip().splitlines()[-1][:120] if r.stderr else "")
        continue
    c = subprocess.run([PY, "-c", CHECK], capture_output=True, text=True, timeout=60)
    print(v, c.stdout.strip() or c.stderr.strip()[:200])
