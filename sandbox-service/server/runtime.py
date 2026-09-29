"""Docker runtime management for sandbox instances (control plane side).

P1: feature-driven services (envd / jupyter / browser) + third host port for :3000.
P2: network allowlist (iptables) + CRIU checkpoint/restore for pause/resume.
"""
import json
import logging
import os
import threading
import time

import docker
import httpx

import netpolicy

log = logging.getLogger("runtime")

client = docker.DockerClient(base_url="unix://var/run/docker.sock")

CHECKPOINT_DIR = os.getenv("CHECKPOINT_DIR", "/var/lib/sbx-checkpoints")
CRIU_ENABLED = os.getenv("CRIU_ENABLED", "auto")  # auto | on | off
NETWORK_NAME = "sandbox-net"

_criu_state = {"checked": False, "ok": False}
# Guards _criu_probe() and _criu_state writes. Without this, the startup
# warmup and a concurrent /health hit both see checked=False and race on the
# shared `sbx-criuprobe` container — the loser's failed probe overwrites the
# winner's True cache with False, leaving criu_available() reporting False
# forever despite successful capability.
_criu_lock = threading.Lock()


# ---------------- infra ----------------

def ensure_network():
    try:
        client.networks.get(NETWORK_NAME)
    except docker.errors.NotFound:
        client.networks.create(NETWORK_NAME, driver="bridge")


def features_for(template: dict) -> str:
    if template.get("browser_enabled"):
        return "envd,jupyter,browser"
    return "envd,jupyter"


# ---------------- container lifecycle ----------------

def start_sandbox(sandbox_id: str, template: dict, host_ports: list, envd_token: str, env_vars: dict) -> str:
    """Create + start the container. Returns container name.

    NOTE: Uses --network=host so each container binds directly to the host's
    allocated ports (via ENVD_PORT / JUPYTER_PORT / BROWSER_PORT env vars).
    This is required for CRIU restore compatibility on Docker 29 — the bridge
    networking path has a netns bind-mount bug that prevents `docker start
    --checkpoint` from restoring the container's network namespace.
    """
    name = f"sbx-{sandbox_id}"
    image = template["image"]
    cpu = float(template["cpu_count"] or 1)
    mem = f"{int(template['memory_mb'])}m"
    envs = {**json.loads(template.get("env_vars") or "{}"), **(env_vars or {})}
    envs["ENVD_TOKEN"] = envd_token
    envs["SANDBOX_ID"] = sandbox_id

    features = features_for(template)
    envs["SBX_FEATURES"] = features
    # With --network=host the container binds directly to these host ports.
    envs["ENVD_PORT"] = str(host_ports[0])
    if "jupyter" in features:
        envs["JUPYTER_PORT"] = str(host_ports[1])
    if "browser" in features:
        envs["BROWSER_PORT"] = str(host_ports[2])

    client.containers.run(
        image,
        name=name,
        hostname=sandbox_id,
        detach=True,
        network_mode="host",
        nano_cpus=int(cpu * 1e9),
        mem_limit=mem,
        environment=envs,
        labels={"sandbox-service": "1", "sandbox-id": sandbox_id},
        auto_remove=False,
        stdin_open=True,
        tty=False,
    )

    ip = container_ip(sandbox_id)
    policy = json.loads(template.get("network_policy") or '{"mode":"open"}')
    # NOTE: With --network=host, all containers share 127.0.0.1, so IP-based
    # iptables rules can't isolate per-container. Netpolicy is only effective
    # when the container has its own network namespace (bridge mode).
    if policy.get("mode") != "open" and ip != "127.0.0.1":
        try:
            netpolicy.apply(sandbox_id, ip, policy)
        except Exception:
            log.exception("netpolicy apply failed for %s", sandbox_id)
    return name


def container_ip(sandbox_id: str):
    """Return the IP the data-plane services listen on.

    With --network=host the container shares the host's network stack, so all
    services are reachable on 127.0.0.1 (the edge proxy is also on the host).
    For backward compatibility with bridge-mode containers, we still try to
    read the sandbox-net IP first.
    """
    try:
        c = client.containers.get(f"sbx-{sandbox_id}")
        nets = c.attrs.get("NetworkSettings", {}).get("Networks", {})
        ip = nets.get(NETWORK_NAME, {}).get("IPAddress") or None
        if ip:
            return ip
    except docker.errors.NotFound:
        pass
    return "127.0.0.1"


def wait_envd(host_port: int, timeout_s: float = 25.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            r = httpx.get(f"http://127.0.0.1:{host_port}/health", timeout=2.0)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def wait_browser(host_port: int, timeout_s: float = 60.0) -> bool:
    """Poll the browser service /health until it reports ok (Chromium CDP ready)."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            r = httpx.get(f"http://127.0.0.1:{host_port}/health", timeout=3.0)
            if r.status_code == 200 and r.json().get("ok"):
                return True
        except Exception:
            pass
        time.sleep(1.0)
    return False


def stop_container(sandbox_id: str, remove: bool = False):
    name = f"sbx-{sandbox_id}"
    try:
        c = client.containers.get(name)
        ip = container_ip(sandbox_id)
        c.stop(timeout=5)
        try:
            netpolicy.revoke(sandbox_id, ip)
        except Exception:
            pass
        if remove:
            c.remove(force=True)
        return True
    except docker.errors.NotFound:
        return False


def remove_container(sandbox_id: str):
    name = f"sbx-{sandbox_id}"
    ip = container_ip(sandbox_id)
    try:
        netpolicy.revoke(sandbox_id, ip)
    except Exception:
        pass
    try:
        c = client.containers.get(name)
        c.remove(force=True)
        _checkpoint_delete(name, "sbx-snap")
        return True
    except docker.errors.NotFound:
        return False


def container_running(sandbox_id: str) -> bool:
    try:
        c = client.containers.get(f"sbx-{sandbox_id}")
        return c.status == "running"
    except docker.errors.NotFound:
        return False


# ---------------- lifecycle hooks (P3) ----------------

import threading as _threading


def exec_hook(container_name: str, hook: dict) -> dict:
    """Run a hook inside the container via `docker exec`.

    Watchdog is enforced via wall-clock timeout. On timeout, returns
    {ok: False, error: 'timeout after Ns}s'} but does NOT kill the in-flight
    command — the hook author is expected to use `timeout` themselves for
    hard-kill semantics. (docker SDK doesn't expose a clean cancellation API
    for exec_run.)
    """
    timeout_s = max(1, int(hook.get("timeout_s") or 60))
    cmd = ["/bin/sh", "-c", hook["command"]]
    env = hook.get("env") or None
    cwd = hook.get("cwd") or None
    box: dict = {"done": False, "out": None, "err": None}

    def _run():
        try:
            c = client.containers.get(container_name)
            res = c.exec_run(cmd, environment=env, workdir=cwd,
                             demux=True, stream=False, detach=False)
            box["out"] = res
        except Exception as e:
            box["err"] = str(e)
        finally:
            box["done"] = True

    t = _threading.Thread(target=_run, daemon=True)
    t0 = time.time()
    t.start()
    t.join(timeout_s)
    elapsed = time.time() - t0
    if not box["done"]:
        log.warning("hook %r in %s timed out after %ss", hook.get("name"), container_name, timeout_s)
        return {"ok": False, "exit_code": -1, "error": f"timeout after {timeout_s}s",
                "stdout": "", "stderr": "", "elapsed_s": round(elapsed, 2)}
    if box["err"]:
        return {"ok": False, "exit_code": -1, "error": box["err"],
                "stdout": "", "stderr": "", "elapsed_s": round(elapsed, 2)}
    res = box["out"]
    out, err = (b"", b"")
    if isinstance(res.output, tuple):
        out, err = res.output
    elif isinstance(res.output, bytes):
        out = res.output
    return {
        "ok": res.exit_code == 0,
        "exit_code": res.exit_code,
        "stdout": (out or b"").decode("utf-8", "replace")[-2000:],
        "stderr": (err or b"").decode("utf-8", "replace")[-2000:],
        "elapsed_s": round(elapsed, 2),
    }


def run_startup_hooks(sandbox_id: str, hooks: list) -> dict:
    """Run a list of startup hooks sequentially.

    Returns a per-hook result list and an aggregate summary:
      { results: [{name, ok, exit_code, error?, elapsed_s, stdout, stderr}, ...],
        all_passed: bool,
        blocking_failures: [names whose fail_closed=true AND exit_code!=0] }
    Stops at the first fail_closed=True failure so subsequent hooks don't
    pile on top of a broken sandbox.
    """
    name = f"sbx-{sandbox_id}"
    results = []
    blocking = []
    for h in hooks or []:
        if not isinstance(h, dict) or not h.get("command"):
            results.append({"name": h.get("name", "?"), "ok": False,
                            "exit_code": -1, "error": "invalid hook (missing command)"})
            continue
        log.info("running startup hook %r in %s", h.get("name"), sandbox_id)
        r = exec_hook(name, h)
        r["name"] = h.get("name", "?")
        results.append(r)
        if not r["ok"] and h.get("fail_closed", True):
            blocking.append(r["name"])
            break
    return {"results": results, "all_passed": not blocking, "blocking_failures": blocking}


def run_periodic_hooks(sandbox_id: str, hooks: list, state: dict, now: float) -> dict:
    """Run periodic hooks that are due, return the updated state.

    `state` is a per-sandbox dict keyed by hook name with:
        { last_run: float, last_ok: bool, last_error: str, elapsed_s: float }
    A hook is "due" when its `interval_s` (default 60) has elapsed since last_run.
    Failures never abort the schedule — periodic hooks are best-effort.
    Returns: { ran: [names], state: new_state }
    """
    name = f"sbx-{sandbox_id}"
    ran = []
    state = dict(state or {})
    for h in hooks or []:
        if not isinstance(h, dict) or not h.get("command"):
            continue
        hname = h.get("name", "?")
        interval = max(1, int(h.get("interval_s") or 60))
        prev = state.get(hname, {}) or {}
        last_run = float(prev.get("last_run") or 0)
        if now - last_run < interval:
            continue
        log.info("running periodic hook %r in %s", hname, sandbox_id)
        r = exec_hook(name, h)
        state[hname] = {
            "last_run": now,
            "last_ok": r["ok"],
            "last_error": r.get("error") or "",
            "elapsed_s": r.get("elapsed_s", 0.0),
        }
        ran.append(hname)
    return {"ran": ran, "state": state}


def start_container(sandbox_id: str, template: dict = None) -> bool:
    try:
        c = client.containers.get(f"sbx-{sandbox_id}")
        if c.status != "running":
            c.start()
        if template:
            policy = json.loads(template.get("network_policy") or '{"mode":"open"}')
            ip = container_ip(sandbox_id)
            if policy.get("mode") != "open" and ip != "127.0.0.1":
                netpolicy.apply(sandbox_id, ip, policy)
        return True
    except docker.errors.NotFound:
        return False


# ---------------- CRIU (P2) ----------------

def _docker_api(method: str, path: str, body=None, params=None, timeout=120):
    transport = httpx.HTTPTransport(uds="/var/run/docker.sock")
    with httpx.Client(transport=transport, timeout=timeout) as c:
        return c.request(method, f"http://docker{path}", json=body, params=params)


def _criu_probe() -> bool:
    """Real capability test: dump + restore a throwaway container.

    Docker 29 with the containerd snapshotter breaks CRIU restore. Even with
    the snapshotter disabled, bridge-network containers fail to restore due to
    a netns bind-mount bug (`/proc/0/ns/net -> ...`). We therefore test with
    --network=host, which is what sandbox containers actually use. The probe
    uses a glibc-based image (python:3.11-slim) because CRIU on alpine/musl
    has separate restore issues.
    """
    name = "sbx-criuprobe"
    try:
        try:
            client.containers.get(name).remove(force=True)
        except docker.errors.NotFound:
            pass
        c = client.containers.run("m.daocloud.io/docker.io/library/python:3.11-slim",
                                  name=name, command="sleep 120", detach=True,
                                  network_mode="host")
        time.sleep(1.5)
        dumped = checkpoint_container_probe(name, "probe")
        restored = False
        if dumped:
            restored = _restore_probe(name, "probe")
        try:
            client.containers.get(name).remove(force=True)
        except Exception:
            pass
        ok = bool(dumped and restored)
        log.info("criu capability probe: dump=%s restore=%s -> %s", dumped, restored, ok)
        return ok
    except Exception as e:
        log.warning("criu probe error: %s", e)
        try:
            client.containers.get(name).remove(force=True)
        except Exception:
            pass
        return False


def checkpoint_container_probe(container_name: str, checkpoint_id: str) -> bool:
    try:
        r = _docker_api("POST", f"/containers/{container_name}/checkpoints",
                        body={"CheckpointID": checkpoint_id, "LeaveRunning": False}, timeout=120)
        return r.status_code < 400
    except Exception:
        return False


def _restore_probe(container_name: str, checkpoint_id: str) -> bool:
    try:
        # NOTE: Docker 29 rejects `--checkpoint-dir` on `docker start`, so we do
        # not pass it here. The checkpoint lives in Docker's default location
        # (/var/lib/docker/containers/<id>/checkpoints/<name>/).
        r = _docker_api("POST", f"/containers/{container_name}/start",
                        params={"checkpoint": checkpoint_id}, timeout=120)
        if r.status_code >= 400:
            log.info("criu restore probe failed: %s", r.text[:200])
            return False
        time.sleep(2)
        c = client.containers.get(container_name)
        return c.status == "running"
    except Exception:
        return False


def criu_available(force=False) -> bool:
    if force:
        # Force re-probe under lock; bypass cache.
        with _criu_lock:
            ok = _criu_probe()
            _criu_state.update(checked=True, ok=bool(ok))
            return _criu_state["ok"]
    if _criu_state["checked"]:
        return _criu_state["ok"]
    if CRIU_ENABLED == "off":
        _criu_state.update(checked=True, ok=False)
        return False
    if CRIU_ENABLED == "on":
        _criu_state.update(checked=True, ok=True)
        return True
    with _criu_lock:
        # Double-check after acquiring the lock — another thread may have
        # already probed and cached a result while we were waiting.
        if _criu_state["checked"]:
            return _criu_state["ok"]
        ok = _criu_probe()  # auto: verify with a real round-trip
        _criu_state.update(checked=True, ok=bool(ok))
        return _criu_state["ok"]


def _checkpoint_delete(container_name: str, checkpoint_id: str):
    try:
        _docker_api("DELETE", f"/containers/{container_name}/checkpoints/{checkpoint_id}",
                    params={"checkpoint-dir": CHECKPOINT_DIR}, timeout=30)
    except Exception:
        pass


def checkpoint_container(sandbox_id: str, checkpoint_id: str = "sbx-snap") -> bool:
    """Dump the running container's memory state (docker checkpoint). Falls back to False."""
    if not criu_available():
        return False
    name = f"sbx-{sandbox_id}"
    try:
        # Don't pass CheckpointDir — Docker 29 rejects it on the restore side,
        # and we want create+restore to agree on the default location.
        r = _docker_api("POST", f"/containers/{name}/checkpoints",
                        body={"CheckpointID": checkpoint_id, "LeaveRunning": False},
                        timeout=180)
        if r.status_code >= 400:
            log.warning("checkpoint create failed [%s]: %s", r.status_code, r.text[:300])
            return False
        return True
    except Exception as e:
        log.warning("checkpoint create error: %s", e)
        return False


def restore_container(sandbox_id: str, checkpoint_id: str = "sbx-snap") -> bool:
    """Restore a previously checkpointed container (start --checkpoint)."""
    if not criu_available():
        return False
    name = f"sbx-{sandbox_id}"
    try:
        # Don't pass `checkpoint-dir` — Docker 29 rejects it.
        r = _docker_api("POST", f"/containers/{name}/start",
                        params={"checkpoint": checkpoint_id},
                        timeout=180)
        if r.status_code >= 400:
            log.warning("checkpoint restore failed [%s]: %s", r.status_code, r.text[:300])
            return False
        return True
    except Exception as e:
        log.warning("checkpoint restore error: %s", e)
        return False


def list_checkpoints(sandbox_id: str):
    try:
        r = _docker_api("GET", f"/containers/sbx-{sandbox_id}/checkpoints",
                        timeout=30)
        return r.json() if r.status_code == 200 else []
    except Exception:
        return []
