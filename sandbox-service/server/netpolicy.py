"""P2: per-sandbox egress allowlist (network white list) implemented with iptables.

Rules are installed in the Docker-provided DOCKER-USER chain (traversed from the
FORWARD hook before Docker's own rules), scoped by the container's source IP:

    -A DOCKER-USER -s <container-ip> -j SBX_<id>
    -N SBX_<id>
      -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
      -p udp --dport 53 -j ACCEPT            (DNS: required to resolve allowlisted domains)
      -p tcp --dport 53 -j ACCEPT
      -d <resolved-ip> -j ACCEPT             (one per allowlisted domain / cidr)
      -j DROP                                (everything else)

policy = {"mode": "open" | "allowlist" | "blocked", "domains": [...], "cidrs": [...]}
"""
import json
import logging
import socket
import subprocess

log = logging.getLogger("netpolicy")


def _run(args, timeout=10):
    return subprocess.run(["iptables", *args], capture_output=True, text=True, timeout=timeout)


def chain_name(sandbox_id: str) -> str:
    # iptables chain names are capped at 28 chars
    tail = sandbox_id.replace("-", "")[-10:]
    return f"SBX_{tail}"


def resolve_domains(domains):
    resolved = {}
    for d in domains or []:
        try:
            infos = socket.getaddrinfo(d, None, proto=socket.IPPROTO_TCP)
        except Exception as e:
            log.warning("resolve failed for %s: %s", d, e)
            resolved[d] = []
            continue
        ips = sorted({i[4][0] for i in infos})
        resolved[d] = ips
    return resolved


def _ensure_chain(chain: str) -> bool:
    r = _run(["-t", "filter", "-n", chain])  # -n fails if it exists
    if r.returncode != 0:
        r2 = _run(["-t", "filter", "-N", chain])
        if r2.returncode != 0:
            log.error("cannot create chain %s: %s", chain, r2.stderr.strip())
            return False
    return True


def apply(sandbox_id: str, container_ip: str, policy: dict):
    """Install the egress rules. Returns a status dict."""
    policy = policy or {"mode": "open"}
    mode = policy.get("mode", "open")
    if mode == "open" or not container_ip:
        return {"mode": "open", "applied": False}

    chain = chain_name(sandbox_id)
    if not _ensure_chain(chain):
        return {"mode": mode, "applied": False, "error": "iptables unavailable"}
    _run(["-t", "filter", "-F", chain])

    _run(["-t", "filter", "-A", chain, "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"])
    _run(["-t", "filter", "-A", chain, "-p", "udp", "--dport", "53", "-j", "ACCEPT"])
    _run(["-t", "filter", "-A", chain, "-p", "tcp", "--dport", "53", "-j", "ACCEPT"])

    allowed = []
    if mode == "allowlist":
        resolved = resolve_domains(policy.get("domains"))
        for cidrs in (policy.get("cidrs") or []):
            allowed.append(cidrs)
        for ips in resolved.values():
            allowed.extend(ips)
        for target in sorted(set(allowed)):
            r = _run(["-t", "filter", "-A", chain, "-d", target, "-j", "ACCEPT"])
            if r.returncode != 0:
                log.warning("bad allowlist target %s: %s", target, r.stderr.strip())
        drop = _run(["-t", "filter", "-A", chain, "-j", "DROP"])
    else:  # blocked
        drop = _run(["-t", "filter", "-A", chain, "-j", "DROP"])

    # hook into DOCKER-USER (idempotent: remove any stale rule first)
    revoke_hook(sandbox_id, container_ip)
    r = _run(["-t", "filter", "-I", "DOCKER-USER", "1", "-s", container_ip, "-j", chain])
    if r.returncode != 0:
        return {"mode": mode, "applied": False, "error": r.stderr.strip()}

    log.info("netpolicy %s applied chain=%s ip=%s mode=%s allowed=%s", sandbox_id, chain, container_ip, mode, allowed)
    return {"mode": mode, "applied": True, "chain": chain, "allowed": allowed, "drop": drop.returncode == 0}


def revoke_hook(sandbox_id: str, container_ip: str):
    chain = chain_name(sandbox_id)
    if container_ip:
        while True:
            r = _run(["-t", "filter", "-D", "DOCKER-USER", "-s", container_ip, "-j", chain])
            if r.returncode != 0:
                break


def revoke(sandbox_id: str, container_ip: str = None):
    chain = chain_name(sandbox_id)
    revoke_hook(sandbox_id, container_ip)
    _run(["-t", "filter", "-F", chain])
    _run(["-t", "filter", "-X", chain])
    return {"revoked": True, "chain": chain}


def status(sandbox_id: str):
    chain = chain_name(sandbox_id)
    r = _run(["-t", "filter", "-S", chain])
    return {"chain": chain, "exists": r.returncode == 0, "rules": r.stdout.strip().splitlines()}


def supported() -> bool:
    try:
        r = _run(["-t", "filter", "-L", "DOCKER-USER", "-n"], timeout=8)
        return r.returncode == 0
    except Exception as e:
        log.warning("iptables unsupported: %s", e)
        return False
