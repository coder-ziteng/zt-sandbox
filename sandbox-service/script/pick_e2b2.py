"""探测支持 api_headers/api_url 的 e2b 2.x 版本。"""
import subprocess
import sys

PY = sys.executable
CANDIDATES = ["2.0.0", "2.0.1", "2.1.0", "2.2.0", "2.2.1", "2.3.0", "2.4.0", "2.4.1", "2.5.0"]

CHECK = r"""
import json
try:
    from e2b.connection_config import ApiParams
    fields = list(getattr(ApiParams, "__annotations__", {}).keys())
except Exception as e:
    fields = ["err:" + str(e)]
from e2b import Sandbox
posts = ""
try:
    import pkgutil
    import e2b.api.client.api.sandboxes as sb
    posts = ",".join([m.name for m in pkgutil.iter_modules(sb.__path__) if "post" in m.name])
except Exception as e:
    posts = "err"
print(json.dumps({"api_params": fields, "create": hasattr(Sandbox, "create"), "v2": "post_v2_sandboxes" in posts}))
"""

for v in CANDIDATES:
    r = subprocess.run([PY, "-m", "pip", "install", "-q", "--force-reinstall", "--no-deps", f"e2b=={v}"],
                       capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        print(v, "INSTALL_FAIL")
        continue
    c = subprocess.run([PY, "-c", CHECK], capture_output=True, text=True, timeout=60)
    print(v, c.stdout.strip() or c.stderr.strip()[:150])
