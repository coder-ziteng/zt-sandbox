"""End-to-end smoke test for the MCP server.

Spawns mcp_server.py as a subprocess over stdio and walks all major tools.
Run from the project root:

    python -m tests.mcp_smoke
or
    python tests/mcp_smoke.py

Required env:
  SBX_API_URL    — control plane base URL
  SBX_API_KEY    — control plane API key (Bearer)

Optional:
  SBX_TEMPLATE  — template code to use (defaults to first one returned by /v2/templates)
  MCP_SERVER    — path to mcp_server.py (defaults to ../server/mcp_server.py)
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SERVER = os.environ.get("MCP_SERVER") or str(_HERE.parent / "server" / "mcp_server.py")
assert Path(_SERVER).exists(), f"mcp_server.py not found: {_SERVER}"

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
except ImportError:
    sys.stderr.write("ERROR: `mcp` package required: pip install 'mcp>=1.0'\n")
    sys.exit(2)


async def _call(session: ClientSession, name: str, **kwargs) -> str:
    print(f"  → {name}({', '.join(f'{k}={v!r:.40}' for k, v in kwargs.items())})")
    t0 = time.time()
    res = await session.call_tool(name, kwargs or None)
    dt = time.time() - t0
    if res.isError:
        raise SystemExit(f"  ✗ {name} errored in {dt:.2f}s: {res.content}")
    text = "\n".join(c.text for c in res.content if c.type == "text")
    snippet = text[:200].replace("\n", " ⏎ ")
    print(f"  ✓ {name} {dt:.2f}s :: {snippet}{'...' if len(text) > 200 else ''}")
    return text


async def main():
    api_url = os.environ.get("SBX_API_URL") or "http://127.0.0.1:8902"
    api_key = os.environ["SBX_API_KEY"]
    template = os.environ.get("SBX_TEMPLATE")

    params = StdioServerParameters(
        command=sys.executable,
        args=[_SERVER],
        env={**os.environ, "SBX_API_URL": api_url, "SBX_API_KEY": api_key},
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            t0 = time.time()
            await session.initialize()
            print(f"MCP handshake OK ({time.time() - t0:.2f}s)")

            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            print(f"tools registered: {len(names)}")
            assert len(names) == 12, f"expected 12 tools, got {len(names)}: {names}"

            # 1. List templates & pick one
            tpl_text = await _call(session, "list_templates")
            if not template:
                # First tmpl_xxx in the summary
                for line in tpl_text.splitlines():
                    if "tmpl_" in line:
                        template = line.split()[0].lstrip("-")
                        break
                if not template:
                    raise SystemExit(f"could not derive template from:\n{tpl_text}")
            print(f"  using template: {template}")

            # 2. Create sandbox
            create_text = await _call(session, "create_sandbox", template_id=template, timeout=300)
            sid = None
            for line in create_text.splitlines():
                if line.startswith("sandbox_id="):
                    sid = line.split("=", 1)[1].strip()
                    break
            assert sid and sid.startswith("sbx"), f"no sandbox_id in: {create_text}"
            print(f"  → sandbox_id = {sid}")

            try:
                # 3. list / get
                await _call(session, "list_sandboxes")
                await _call(session, "get_sandbox", sandbox_id=sid)

                # 4. files_write → files_read → files_list round trip
                marker_path = "/home/user/workspace/.mcp_smoke.txt"
                marker_body = f"hello from mcp smoke @ {time.time()}"
                await _call(session, "files_write",
                            sandbox_id=sid, path=marker_path, content=marker_body)
                read_back = await _call(session, "files_read",
                                        sandbox_id=sid, path=marker_path)
                assert marker_body in read_back, f"round-trip failed: {read_back!r}"
                await _call(session, "files_list",
                            sandbox_id=sid, path="/home/user")

                # 5. run_code (stateful across two calls)
                await _call(session, "run_code",
                            sandbox_id=sid, code="x = 41\nprint('first', x)")
                rc2 = await _call(session, "run_code",
                                  sandbox_id=sid, code="print(x + 1)")
                assert "42" in rc2, f"stateful exec failed: {rc2!r}"

                # 6. run_command
                await _call(session, "run_command",
                            sandbox_id=sid,
                            command="echo hello && uname -a && pwd",
                            cwd="/home/user")

                # 7. pause → resume.  Stateful retention (43) only if CRIU is active;
                #    otherwise pause_mode=stop drops the in-memory kernel.
                pause_msg = await _call(session, "pause_sandbox", sandbox_id=sid)
                await _call(session, "resume_sandbox", sandbox_id=sid, timeout=300)
                rc3 = await _call(session, "run_code",
                                  sandbox_id=sid, code="print('still alive', x + 2)")
                if "criu" in pause_msg.lower():
                    assert "43" in rc3, f"resume lost state under CRIU: {rc3!r}"
                else:
                    # stop-mode: kernel restarted, x is undefined → NameError is expected
                    assert "NameError" in rc3 or "name 'x' is not defined" in rc3, \
                        f"unexpected resume output under stop mode: {rc3!r}"
                    print("  (pause fell back to docker stop — in-memory state dropped, as expected)")
            finally:
                # 8. kill (always clean up)
                await _call(session, "kill_sandbox", sandbox_id=sid)

    print("\nALL MCP SMOKE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())