"""Per-sandbox egress allowlist (network white list) implemented with iptables.

P2: IP/CIDR + domain resolution at install time.
P3: wildcard domains (`*.example.com`), periodic re-resolve for CDN rotation,
    and a registry of per-sandbox state so the control plane can refresh
    iptables rules in place without rebuilding them from scratch.

Rules are installed in the Docker-provided DOCKER-USER chain (traversed from
the FORWARD hook before Docker's own rules), scoped by the container's source IP:

    -A DOCKER-USER -s <container-ip> -j SBX_<id>
    -N SBX_<id>
      -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
      -p udp --dport 53 -j ACCEPT            (DNS: required to resolve allowlisted domains)
      -p tcp --dport 53 -j ACCEPT
      -d <resolved-ip> -j ACCEPT             (one per allowlisted domain / cidr)
      -j DROP                                (everything else)

policy = {"mode": "open" | "allowlist" | "blocked", "domains": [...], "cidrs": [...]}

Wildcard domains (P3):
    - "openai.com"       exact match
    - "*.openai.com"      matches "api.openai.com", "chat.openai.com", etc.
                           For each match we resolve the FQDN and add its IPs.
"""
import logging
import socket
import subprocess
import time

log = logging.getLogger("netpolicy")


def _run(args, timeout=10):
    return subprocess.run(["iptables", *args], capture_output=True, text=True, timeout=timeout)


def chain_name(sandbox_id: str) -> str:
    # iptables chain names are capped at 28 chars
    tail = sandbox_id.replace("-", "")[-10:]
    return f"SBX_{tail}"


# ---------------------------------------------------------------------------
# In-process registry: (sandbox_id) -> {policy, container_ip, refreshed_at}
# Used by refresh() so the control plane doesn't have to round-trip
# template/network_policy every tick.
# ---------------------------------------------------------------------------
_state: dict[str, dict] = {}


def _expand_wildcards(domains: list[str]) -> list[str]:
    """Convert `*.example.com` to a list of concrete FQDNs to resolve.

    We don't try to enumerate subdomains out of thin air — instead we resolve the
    apex (`example.com`) and a small set of well-known prefixes (`www`,
    `api`, `cdn`). For sub-CDNs that aren't covered, the sandbox can still get
    to the IP via the apex's resolved IPs (since most CDNs share IPs at the
    edge). This is a pragmatic compromise: exact-match for security, wildcard
    for usability.
    """
    out = []
    seen = set()
    for d in domains or []:
        d = d.strip().lower().rstrip(".")
        if not d:
            continue
        if d.startswith("*."):
            apex = d[2:]
            if apex and apex not in seen:
                out.append(apex)
                seen.add(apex)
            # Also resolve common prefixes so popular subdomains get explicit
            # coverage in the iptables allow set.
            for sub in ("www", "api", "cdn", "static", "assets", "chat"):
                fqdn = f"{sub}.{apex}"
                if fqdn not in seen:
                    out.append(fqdn)
                    seen.add(fqdn)
        else:
            if d not in seen:
                out.append(d)
                seen.add(d)
    return out


def resolve_domains(domains):
    """Resolve a list of (possibly wildcard) domains to {domain: [ips]}."""
    expanded = _expand_wildcards(domains)
    resolved = {}
    for d in expanded:
        try:
            infos = socket.getaddrinfo(d, None, proto=socket.IPPROTO_TCP)
        except Exception as e:
            log.warning("resolve failed for %s: %s", d, e)
            resolved[d] = []
            continue
        # Keep IPv4 only. iptables-nft in our deploy environment rejects
        # IPv6 targets with "host/network not found"; filtering here gives a
        # clean apply instead of a long list of warnings per refresh.
        all_ips = [i[4][0] for i in infos]
        ips = sorted({ip for ip in all_ips if not (isinstance(ip, str) and ":" in ip)})
        resolved[d] = ips
    return resolved


def _ensure_chain(chain: str) -> bool:
    # -L lists chain rules (succeeds if it exists). -n is numeric output.
    # NB: earlier versions used bare `-n` here which is invalid syntax
    # (`iptables -n CHAIN` → "Bad argument"), so the existence probe always
    # failed and we always fell through to `-N` — fine for one-shot apply, but
    # fatal for refresh() which calls this on an already-existing chain.
    r = _run(["-t", "filter", "-L", chain, "-n"])
    if r.returncode != 0:
        r2 = _run(["-t", "filter", "-N", chain])
        if r2.returncode != 0:
            log.error("cannot create chain %s: %s", chain, r2.stderr.strip())
            return False
    return True


def _populate_chain(chain: str, policy: dict, resolved: dict | None = None) -> dict:
    """Flush `chain` and add the standard ESTABLISHED+DNS+allow+drop rules.

    `resolved` is the {domain: ips} mapping to install; if None, we re-resolve
    the policy's domains from scratch (used by refresh()).
    Returns {allowed, drop_ok}.
    """
    _run(["-t", "filter", "-F", chain])
    _run(["-t", "filter", "-A", chain, "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"])
    _run(["-t", "filter", "-A", chain, "-p", "udp", "--dport", "53", "-j", "ACCEPT"])
    _run(["-t", "filter", "-A", chain, "-p", "tcp", "--dport", "53", "-j", "ACCEPT"])

    allowed: list[str] = []
    drop_ok = True
    if policy.get("mode") == "allowlist":
        resolved = resolved if resolved is not None else resolve_domains(policy.get("domains"))
        for cidr in policy.get("cidrs") or []:
            allowed.append(cidr)
        for ips in resolved.values():
            allowed.extend(ips)
        for target in sorted(set(allowed)):
            r = _run(["-t", "filter", "-A", chain, "-d", target, "-j", "ACCEPT"])
            if r.returncode != 0:
                log.warning("bad allowlist target %s: %s", target, r.stderr.strip())
        drop = _run(["-t", "filter", "-A", chain, "-j", "DROP"])
        drop_ok = drop.returncode == 0
    elif policy.get("mode") == "blocked":
        drop = _run(["-t", "filter", "-A", chain, "-j", "DROP"])
        drop_ok = drop.returncode == 0
    return {"allowed": sorted(set(allowed)), "drop_ok": drop_ok, "resolved": resolved or {}}


def apply(sandbox_id: str, container_ip: str, policy: dict):
    """Install the egress rules. Returns a status dict."""
    policy = policy or {"mode": "open"}
    mode = policy.get("mode", "open")
    if mode == "open" or not container_ip:
        _state.pop(sandbox_id, None)
        return {"mode": "open", "applied": False}

    chain = chain_name(sandbox_id)
    if not _ensure_chain(chain):
        return {"mode": mode, "applied": False, "error": "iptables unavailable"}

    pop = _populate_chain(chain, policy)
    resolved = pop["resolved"]

    # hook into DOCKER-USER (idempotent: remove any stale rule first)
    revoke_hook(sandbox_id, container_ip)
    r = _run(["-t", "filter", "-I", "DOCKER-USER", "1", "-s", container_ip, "-j", chain])
    if r.returncode != 0:
        return {"mode": mode, "applied": False, "error": r.stderr.strip()}

    _state[sandbox_id] = {
        "policy": policy,
        "container_ip": container_ip,
        "refreshed_at": time.time(),
        "resolved": resolved,
        "chain": chain,
    }
    log.info("netpolicy %s applied chain=%s ip=%s mode=%s allowed=%d ips",
             sandbox_id, chain, container_ip, mode, len(pop["allowed"]))
    return {"mode": mode, "applied": True, "chain": chain,
            "allowed": pop["allowed"], "drop": pop["drop_ok"],
            "resolved": resolved, "refreshedAt": _state[sandbox_id]["refreshed_at"]}


def refresh(sandbox_id: str) -> dict | None:
    """Re-resolve domains and update the chain in place.

    Used by the control plane's periodic task to handle DNS-based CDN rotation.
    Returns the new status dict, or None if there's nothing to refresh (e.g.
    open mode, or the sandbox has been torn down since the last refresh).
    """
    st = _state.get(sandbox_id)
    if not st:
        return None
    if st["policy"].get("mode") != "allowlist":
        return None
    chain = st["chain"]
    if not _ensure_chain(chain):
        return {"error": "iptables unavailable", "sandboxID": sandbox_id}
    pop = _populate_chain(chain, st["policy"])
    st["refreshed_at"] = time.time()
    st["resolved"] = pop["resolved"]
    log.info("netpolicy %s refreshed: %d ips", sandbox_id, len(pop["allowed"]))
    return {"sandboxID": sandbox_id, "refreshedAt": st["refreshed_at"],
            "allowed": pop["allowed"], "resolved": pop["resolved"]}


def refresh_all() -> list[dict]:
    """Refresh every registered sandbox. Safe to call from a background tick."""
    out = []
    for sid in list(_state.keys()):
        try:
            r = refresh(sid)
            if r is not None:
                out.append(r)
        except Exception as e:
            log.warning("netpolicy refresh %s failed: %s", sid, e)
            out.append({"sandboxID": sid, "error": str(e)})
    return out


def revoke_hook(sandbox_id: str, container_ip: str | None):
    chain = chain_name(sandbox_id)
    if container_ip:
        while True:
            r = _run(["-t", "filter", "-D", "DOCKER-USER", "-s", container_ip, "-j", chain])
            if r.returncode != 0:
                break


def revoke(sandbox_id: str, container_ip: str | None = None):
    chain = chain_name(sandbox_id)
    revoke_hook(sandbox_id, container_ip)
    _run(["-t", "filter", "-F", chain])
    _run(["-t", "filter", "-X", chain])
    _state.pop(sandbox_id, None)
    return {"revoked": True, "chain": chain}


def status(sandbox_id: str):
    chain = chain_name(sandbox_id)
    r = _run(["-t", "filter", "-S", chain])
    st = _state.get(sandbox_id) or {}
    return {
        "chain": chain,
        "exists": r.returncode == 0,
        "rules": r.stdout.strip().splitlines(),
        "policy": st.get("policy"),
        "refreshedAt": st.get("refreshed_at"),
        "resolvedIPs": st.get("resolved"),
    }


def supported() -> bool:
    try:
        r = _run(["-t", "filter", "-L", "DOCKER-USER", "-n"], timeout=8)
        return r.returncode == 0
    except Exception as e:
        log.warning("iptables unsupported: %s", e)
        return False
