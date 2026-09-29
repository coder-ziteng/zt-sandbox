"""SQLite metadata store for sandbox-service (control plane + edge proxy reader).

P1 additions: browser port + per-template browser flag.
P2 additions: network policy per template, pause mode (criu|stop), quota accounting.
"""
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager

DB_PATH = "/data/sandbox.db"

_local = threading.local()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def db():
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _connect()
        _local.conn = conn
    yield conn
    conn.commit()


def _table_cols(c, table):
    return {r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate(c):
    tcols = _table_cols(c, "templates")
    if "browser_enabled" not in tcols:
        c.execute("ALTER TABLE templates ADD COLUMN browser_enabled INTEGER NOT NULL DEFAULT 0")
    if "network_policy" not in tcols:
        c.execute("ALTER TABLE templates ADD COLUMN network_policy TEXT NOT NULL DEFAULT '{}'")
    if "startup_hooks" not in tcols:
        c.execute("ALTER TABLE templates ADD COLUMN startup_hooks TEXT NOT NULL DEFAULT '[]'")
    if "periodic_hooks" not in tcols:
        c.execute("ALTER TABLE templates ADD COLUMN periodic_hooks TEXT NOT NULL DEFAULT '[]'")
    scols = _table_cols(c, "sandboxes")
    if "host_port_browser" not in scols:
        c.execute("ALTER TABLE sandboxes ADD COLUMN host_port_browser INTEGER")
    if "pause_mode" not in scols:
        c.execute("ALTER TABLE sandboxes ADD COLUMN pause_mode TEXT NOT NULL DEFAULT 'stop'")
    if "features" not in scols:
        c.execute("ALTER TABLE sandboxes ADD COLUMN features TEXT NOT NULL DEFAULT 'envd,jupyter'")
    if "last_activity" not in scols:
        c.execute("ALTER TABLE sandboxes ADD COLUMN last_activity REAL NOT NULL DEFAULT 0")
    if "hook_state" not in scols:
        c.execute("ALTER TABLE sandboxes ADD COLUMN hook_state TEXT NOT NULL DEFAULT '{}'")


def init_db():
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS templates (
                code TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                image TEXT NOT NULL,
                cpu_count INTEGER NOT NULL DEFAULT 1,
                memory_mb INTEGER NOT NULL DEFAULT 2048,
                disk_size_mb INTEGER NOT NULL DEFAULT 2048,
                env_vars TEXT NOT NULL DEFAULT '{}',
                version INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sandboxes (
                sandbox_id TEXT PRIMARY KEY,
                template_code TEXT NOT NULL,
                client_id TEXT NOT NULL,
                envd_token TEXT NOT NULL,
                host_port_envd INTEGER NOT NULL,
                host_port_jupyter INTEGER NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}',
                state TEXT NOT NULL DEFAULT 'running',
                started_at REAL NOT NULL,
                end_at REAL NOT NULL,
                envd_version TEXT NOT NULL DEFAULT '0.7.0',
                container_name TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS port_allocations (
                host_port INTEGER PRIMARY KEY,
                sandbox_id TEXT
            );
            CREATE TABLE IF NOT EXISTS builds (
                build_id TEXT PRIMARY KEY,
                template_code TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'building',
                logs TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL
            );
            """
        )
        _migrate(c)


def new_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:17]}"


# ---------- templates ----------

def create_template(name: str, image: str, cpu: int, mem: int, disk: int, envs: dict,
                    browser_enabled: bool = False, network_policy: dict = None,
                    startup_hooks: list = None, periodic_hooks: list = None) -> str:
    code = new_id("tmpl")
    if not browser_enabled:
        lowered = (image or "").lower()
        browser_enabled = ("browser" in lowered) or ("all-in-one" in lowered) or ("allinone" in lowered)
    with db() as c:
        c.execute(
            "INSERT INTO templates (code,name,image,cpu_count,memory_mb,disk_size_mb,env_vars,version,created_at,"
            "browser_enabled,network_policy,startup_hooks,periodic_hooks) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (code, name, image, cpu, mem, disk, json.dumps(envs), 1, time.time(),
             1 if browser_enabled else 0, json.dumps(network_policy or {"mode": "open"}),
             json.dumps(startup_hooks or []), json.dumps(periodic_hooks or [])),
        )
        build_id = new_id("bld")
        c.execute(
            "INSERT INTO builds (build_id,template_code,status,logs,created_at) VALUES (?,?,?,?,?)",
            (build_id, code, "building", "", time.time()),
        )
    with db() as c:
        c.execute("UPDATE builds SET status='ready', logs='image ready' WHERE build_id=?", (build_id,))
    return code


def get_template(code: str):
    with db() as c:
        row = c.execute("SELECT * FROM templates WHERE code=?", (code,)).fetchone()
    return dict(row) if row else None


def update_template_hooks(code: str, startup_hooks: list = None, periodic_hooks: list = None) -> bool:
    """Replace hook lists on a template. Either arg may be None to leave unchanged."""
    sets, vals = [], []
    if startup_hooks is not None:
        sets.append("startup_hooks=?"); vals.append(json.dumps(startup_hooks))
    if periodic_hooks is not None:
        sets.append("periodic_hooks=?"); vals.append(json.dumps(periodic_hooks))
    if not sets:
        return False
    vals.append(code)
    with db() as c:
        c.execute(f"UPDATE templates SET {','.join(sets)} WHERE code=?", vals)
    return True


def list_templates():
    with db() as c:
        rows = c.execute("SELECT * FROM templates ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def delete_template(code: str):
    with db() as c:
        n = c.execute(
            "SELECT COUNT(*) AS n FROM sandboxes WHERE template_code=? AND state IN ('running','paused')",
            (code,),
        ).fetchone()["n"]
        if n:
            return False
        c.execute("DELETE FROM templates WHERE code=?", (code,))
    return True


def get_build(template_code: str, build_id: str):
    with db() as c:
        row = c.execute(
            "SELECT * FROM builds WHERE build_id=? AND template_code=?",
            (build_id, template_code),
        ).fetchone()
    return dict(row) if row else None


# ---------- ports ----------

PORT_RANGE = range(20000, 20990, 3)  # three ports per sandbox: envd, jupyter, browser


def allocate_ports(sandbox_id: str):
    """Reserve 3 consecutive host ports: [envd(49983), jupyter(49999), browser(3000)]."""
    with db() as c:
        used = {r["host_port"] for r in c.execute(
            "SELECT host_port FROM port_allocations WHERE sandbox_id IS NOT NULL"
        ).fetchall()}
        base = None
        for p in PORT_RANGE:
            if p not in used and (p + 1) not in used and (p + 2) not in used:
                base = p
                break
        if base is None:
            raise RuntimeError("no free ports")
        for offset in range(3):
            c.execute(
                "INSERT OR REPLACE INTO port_allocations (host_port, sandbox_id) VALUES (?,?)",
                (base + offset, sandbox_id),
            )
    return base, base + 1, base + 2


def release_ports(sandbox_id: str):
    with db() as c:
        c.execute("UPDATE port_allocations SET sandbox_id=NULL WHERE sandbox_id=?", (sandbox_id,))


def get_host_ports(sandbox_id: str):
    with db() as c:
        rows = c.execute(
            "SELECT host_port FROM port_allocations WHERE sandbox_id=? ORDER BY host_port", (sandbox_id,)
        ).fetchall()
    return [r["host_port"] for r in rows]


# ---------- sandboxes ----------

def create_sandbox(sandbox_id, template_code, client_id, envd_token, ports, metadata, container_name, ttl,
                   features="envd,jupyter"):
    now = time.time()
    browser_port = ports[2] if len(ports) > 2 else None
    with db() as c:
        c.execute(
            "INSERT INTO sandboxes (sandbox_id,template_code,client_id,envd_token,host_port_envd,host_port_jupyter,"
            "host_port_browser,metadata,state,started_at,end_at,container_name,features,last_activity)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                sandbox_id, template_code, client_id, envd_token, ports[0], ports[1], browser_port,
                json.dumps(metadata), "running", now, now + ttl, container_name, features, now,
            ),
        )


def get_sandbox(sandbox_id: str):
    with db() as c:
        row = c.execute("SELECT * FROM sandboxes WHERE sandbox_id=?", (sandbox_id,)).fetchone()
    return dict(row) if row else None


def list_sandboxes(states=("running", "paused")):
    q = ",".join("?" for _ in states)
    with db() as c:
        rows = c.execute(
            f"SELECT * FROM sandboxes WHERE state IN ({q}) ORDER BY started_at DESC", states
        ).fetchall()
    return [dict(r) for r in rows]


def update_sandbox(sandbox_id: str, **fields):
    if not fields:
        return
    cols = ",".join(f"{k}=?" for k in fields)
    with db() as c:
        c.execute(f"UPDATE sandboxes SET {cols} WHERE sandbox_id=?", (*fields.values(), sandbox_id))


def delete_sandbox(sandbox_id: str):
    with db() as c:
        c.execute("DELETE FROM sandboxes WHERE sandbox_id=?", (sandbox_id,))
    release_ports(sandbox_id)


# ---------- quota (P2) ----------

def count_active() -> int:
    with db() as c:
        return c.execute(
            "SELECT COUNT(*) AS n FROM sandboxes WHERE state IN ('running','paused')"
        ).fetchone()["n"]


def committed_resources():
    """Sum of cpu/mem committed to live sandboxes (for admission control)."""
    with db() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(t.cpu_count),0) AS cpu, COALESCE(SUM(t.memory_mb),0) AS mem "
            "FROM sandboxes s JOIN templates t ON t.code=s.template_code "
            "WHERE s.state IN ('running','paused')"
        ).fetchone()
    return {"count": row["n"], "cpu": row["cpu"], "memoryMB": row["mem"]}
